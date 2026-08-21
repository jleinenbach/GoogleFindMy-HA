# tests/test_chrome_driver.py
"""Tests for the Chrome driver helpers using stubbed undetected-chromedriver APIs."""

from __future__ import annotations

import ast
import importlib
import logging
import os
import pathlib
import platform
import shutil
import signal
import subprocess
import sys
import uuid
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
    """Ensure headless options populate the expected Chrome arguments.

    The list is frozen deliberately, including the last two entries. Those relax
    the browser's origin isolation and are periodically proposed for removal;
    ``chrome_driver.get_options`` carries the reasoning. Short version: they only
    ever apply to the manual, user-started extraction browser, never to a Home
    Assistant runtime, and removing them cannot be verified without a real Google
    sign-in with 2FA. If this assertion fails, the flags were changed without
    that measurement.
    """

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    # The frozen list only holds if the ambient environment does not add to it:
    # a developer with the locale variable exported would see this fail for a
    # reason that has nothing to do with the flags it guards.
    monkeypatch.delenv(chrome_driver.ENV_LOGIN_LOCALE, raising=False)

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
    # Neutralize the container signal so the non-container kill/teardown tests
    # observe the cleanup path regardless of the ambient environment; tests that
    # exercise the container guard set it explicitly.
    monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)


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
# _neutralize_uc_finalizer (WinError 6 teardown noise)
# ---------------------------------------------------------------------------


def test_neutralize_preserves_original_cleanup() -> None:
    """The wrapped __del__ still runs uc's cleanup (no leak on failed builds)."""
    cleanup_calls: list[str] = []

    class _FakeChrome:
        def __del__(self) -> None:
            cleanup_calls.append("cleanup")

    chrome_driver._neutralize_uc_finalizer(SimpleNamespace(Chrome=_FakeChrome))

    assert _FakeChrome._gfmy_del_neutralized is True
    # The wrapper delegates to the original __del__ instead of dropping it:
    # this preserves browser/temp-profile cleanup for a partially built driver.
    inst = _FakeChrome.__new__(_FakeChrome)
    assert _FakeChrome.__del__(inst) is None
    assert cleanup_calls == ["cleanup"]


def test_neutralize_suppresses_del_errors() -> None:
    """A WinError-6-style raise from uc's __del__ is swallowed, not propagated."""

    class _FakeChrome:
        def __del__(self) -> None:
            raise OSError(6, "The handle is invalid")

    chrome_driver._neutralize_uc_finalizer(SimpleNamespace(Chrome=_FakeChrome))

    # Invoking the wrapped __del__ must not raise despite the original raising.
    inst = _FakeChrome.__new__(_FakeChrome)
    assert _FakeChrome.__del__(inst) is None


def test_neutralize_skips_import_stub() -> None:
    """The import-failure stub exposes ``Chrome`` as a function, not a class."""

    def _stub_chrome(*, options: object) -> object:  # pragma: no cover - shape only
        return object()

    # Must not raise and must not mark a plain function.
    chrome_driver._neutralize_uc_finalizer(SimpleNamespace(Chrome=_stub_chrome))

    assert not hasattr(_stub_chrome, "_gfmy_del_neutralized")


def test_neutralize_is_idempotent() -> None:
    """A second call does not re-wrap an already-neutralized class."""

    class _FakeChrome:
        def __del__(self) -> None:
            return None

    module = SimpleNamespace(Chrome=_FakeChrome)
    chrome_driver._neutralize_uc_finalizer(module)
    patched = _FakeChrome.__del__
    chrome_driver._neutralize_uc_finalizer(module)

    assert _FakeChrome.__del__ is patched


def test_neutralize_leaves_class_without_own_del() -> None:
    """A uc release without its own ``__del__`` is left untouched (none fabricated)."""

    class _NoDel:
        pass

    chrome_driver._neutralize_uc_finalizer(SimpleNamespace(Chrome=_NoDel))

    assert "__del__" not in _NoDel.__dict__
    assert not hasattr(_NoDel, "_gfmy_del_neutralized")


def test_get_uc_module_neutralizes_finalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_get_uc_module`` neutralizes the finalizer on the freshly loaded module."""

    class _FakeChrome:
        def __del__(self) -> None:  # pragma: no cover - not invoked in this test
            return None

    fake_module = SimpleNamespace(Chrome=_FakeChrome, ChromeOptions=FakeChromeOptions)
    chrome_driver._reset_uc_cache(None)
    monkeypatch.setattr(chrome_driver, "_load_uc", lambda: fake_module)

    chrome_driver._get_uc_module()

    assert _FakeChrome._gfmy_del_neutralized is True


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
        chrome_driver.get_chrome_version("C:/portable/chrome.exe", prefer_binary=True)
        == 149
    )


# ---------------------------------------------------------------------------
# _kill_existing_chrome_processes
# ---------------------------------------------------------------------------


def _stub_pgrep(
    monkeypatch: pytest.MonkeyPatch, pids: list[int]
) -> tuple[list[list[str]], list[tuple[int, int]]]:
    """Route ``pgrep`` to *pids* and record every signal that is sent.

    Returns ``(recorded_commands, recorded_kills)``. The stub answers with a
    complete ``CompletedProcess`` shape on purpose: an incomplete stub would
    raise ``AttributeError`` inside the helper, and both call sites swallow
    exceptions for best-effort cleanup -- the test would then pass while
    exercising nothing.
    """

    commands: list[list[str]] = []
    kills: list[tuple[int, int]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0 if pids else 1,
            stdout="".join(f"{pid}\n" for pid in pids),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return commands, kills


def test_kill_existing_chrome_processes_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)
    calls, kills = _stub_pgrep(monkeypatch, [4242])

    chrome_driver._kill_existing_chrome_processes()

    assert calls == [["pgrep", "-f", "chrome"]]
    assert kills == [(4242, signal.SIGTERM)]


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


def test_kill_existing_chrome_processes_spares_self_and_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the cleanup must never signal itself or one of its ancestors.

    ``pkill -f chrome`` matches the *full command line*, so any process that
    merely carries the word in its argv is a target -- including the caller's
    own ancestry.  Measured on 2026-07-28: a pytest run that received
    ``tests/test_chrome_driver.py`` as an argument died with exit 143
    (SIGTERM) at exactly the test that spawns ``main`` as a subprocess.  The
    A/B control differed only by a ``-k`` filter that matched nothing, i.e. by
    the word in argv alone: without it exit 0, with it exit 143.  The grandchild
    shot its grandparent.
    """

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(chrome_driver.time, "sleep", lambda _s: None)

    run_commands: list[list[str]] = []
    own_pid = os.getpid()
    parent_pid = os.getppid()

    def _fake_run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        run_commands.append(cmd)
        # Emulate pgrep: the pattern matches our own ancestry *and* a stranger.
        return SimpleNamespace(
            returncode=0, stdout=f"{own_pid}\n{parent_pid}\n4242\n", stderr=""
        )

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    chrome_driver._kill_existing_chrome_processes()

    assert not any("pkill" in cmd for cmd in run_commands), (
        f"the broad pattern kill must be gone; commands were {run_commands}"
    )
    assert killed == [(4242, signal.SIGTERM)], (
        f"only the stranger may be signalled, got {killed}"
    )


# ---------------------------------------------------------------------------
# ancestry-aware process cleanup helpers
# ---------------------------------------------------------------------------


def test_protected_pids_contains_self_parent_and_init() -> None:
    """No mocks: the real ``/proc`` walk must reach the whole ancestry."""

    protected = chrome_driver._protected_pids()

    assert os.getpid() in protected
    assert os.getppid() in protected
    assert 1 in protected, f"the walk stopped early: {sorted(protected)}"


def test_protected_pids_falls_back_to_ps_without_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``/proc`` (macOS) the ancestry comes from a single ``ps`` call.

    The table starts at the *real* parent PID on purpose: a walk that uses the
    protected set as its cycle guard aborts on the first step (the parent is
    pre-seeded) and would silently return a two-element set.
    """

    own = os.getpid()
    real_parent = os.getppid()
    table = f"{own} {real_parent}\n{real_parent} 400\n400 1\n1 0\n"
    monkeypatch.setattr(os.path, "isdir", lambda path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout=table, stderr=""),
    )

    protected = chrome_driver._protected_pids()

    assert {own, real_parent, 400, 1} <= protected


def test_protected_pids_refuses_a_cyclic_ppid_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle terminates the walk *and* invalidates the answer.

    A loop proves the PPID data is inconsistent, not that the chain was walked
    to PID 1: some ancestor above the loop is missing from the set, and
    signalling it is precisely the failure this helper prevents. Returning the
    partial set would look like protection while being none.
    """

    own = os.getpid()
    table = f"{own} 5\n5 7\n7 5\n"
    monkeypatch.setattr(os.path, "isdir", lambda path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout=table, stderr=""),
    )

    assert chrome_driver._protected_pids() is None


def test_read_ppid_from_proc_returns_none_without_a_ppid_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A status file without a ``PPid:`` line is unknown, not an error."""

    status = tmp_path / "status"
    status.write_text("Name:\tsomething\nState:\tS (sleeping)\n", encoding="utf-8")
    monkeypatch.setattr(
        "builtins.open", lambda _path, **_k: status.open(encoding="utf-8")
    )

    assert chrome_driver._read_ppid_from_proc(4242) is None


def test_protected_pids_stops_at_the_depth_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathologically deep chain terminates at the limit and refuses to answer.

    Synthetic PIDs start far above ``pid_max`` defaults so they cannot collide
    with this process' real parent, which would make the assertion flaky.
    """

    own = os.getpid()
    depth = chrome_driver._ANCESTRY_MAX_DEPTH
    base = 10**7
    chain = {own: base}
    chain.update({base + step: base + 1 + step for step in range(depth + 10)})
    table = "".join(f"{pid} {ppid}\n" for pid, ppid in chain.items())

    monkeypatch.setattr(os.path, "isdir", lambda path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout=table, stderr=""),
    )

    # A chain that never reaches PID 1 within the limit is an incomplete answer,
    # and an incomplete ancestry filter is what the fix exists to prevent.
    assert chrome_driver._protected_pids() is None


def test_protected_pids_returns_none_when_the_chain_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable ancestry (hidepid, no ps) yields ``None``, not a partial set.

    A partial set is the dangerous answer: it looks like protection while the
    grandparent -- the process that actually died in the measured incident -- is
    already unprotected.
    """

    monkeypatch.setattr(os.path, "isdir", lambda path: True)
    monkeypatch.setattr(chrome_driver, "_read_ppid_from_proc", lambda _pid: None)

    def _no_ps(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("ps missing")

    monkeypatch.setattr(subprocess, "run", _no_ps)

    assert chrome_driver._protected_pids() is None


def test_protected_pids_falls_back_to_ps_when_proc_entry_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/proc`` present but an entry unreadable: ``ps`` completes the chain."""

    own = os.getpid()
    real_parent = os.getppid()
    table = f"{own} {real_parent}\n{real_parent} 400\n400 1\n1 0\n"

    monkeypatch.setattr(os.path, "isdir", lambda path: True)
    monkeypatch.setattr(chrome_driver, "_read_ppid_from_proc", lambda _pid: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout=table, stderr=""),
    )

    protected = chrome_driver._protected_pids()

    assert protected is not None
    assert {own, real_parent, 400, 1} <= protected


def test_terminate_matching_processes_signals_nothing_on_unknown_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an unresolvable ancestry the cleanup is skipped, not run blindly."""

    monkeypatch.setattr(chrome_driver, "_protected_pids", lambda: None)
    monkeypatch.setattr(
        chrome_driver,
        "_pgrep_pids",
        lambda _p: pytest.fail("pgrep must not even be consulted"),
    )

    def _forbidden(_pid: int, _sig: int) -> None:
        pytest.fail("nothing may be signalled without a complete ancestry")

    monkeypatch.setattr(os, "kill", _forbidden)

    assert chrome_driver._terminate_matching_processes("chrome") == 0


def test_ppid_map_from_ps_ignores_malformed_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(
            returncode=0, stdout="10 4\nnot a row\n\n20 x\n30 6\n", stderr=""
        ),
    )

    assert chrome_driver._ppid_map_from_ps() == {10: 4, 30: 6}


def test_ppid_map_from_ps_returns_empty_without_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("ps missing")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert chrome_driver._ppid_map_from_ps() == {}


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, "111\n222\n", [111, 222]),
        (1, "", []),  # pgrep's "no match" is not an error
        (0, "111\nrubbish\n222\n", [111, 222]),
        (2, "111\n", []),  # a real failure yields nothing
    ],
)
def test_pgrep_pids_parses_output(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    expected: list[int],
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=""
        ),
    )

    assert chrome_driver._pgrep_pids("chrome") == expected


def test_pgrep_pids_returns_empty_without_pgrep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("pgrep missing")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert chrome_driver._pgrep_pids("chrome") == []


def test_terminate_matching_processes_skips_pid_one_and_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PID 1 is never a target, and a process that dies mid-flight is no error."""

    monkeypatch.setattr(chrome_driver, "_protected_pids", lambda: frozenset({99}))
    monkeypatch.setattr(chrome_driver, "_pgrep_pids", lambda _p: [1, 99, 500, 501])

    kills: list[int] = []

    def _kill(pid: int, _sig: int) -> None:
        if pid == 500:
            raise ProcessLookupError
        kills.append(pid)

    monkeypatch.setattr(os, "kill", _kill)

    assert chrome_driver._terminate_matching_processes("chrome") == 1
    assert kills == [501]


def test_terminate_matching_processes_tolerates_permission_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _nothing_protected() -> frozenset[int]:
        return frozenset()

    monkeypatch.setattr(chrome_driver, "_protected_pids", _nothing_protected)
    monkeypatch.setattr(chrome_driver, "_pgrep_pids", lambda _p: [700])

    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", _kill)

    assert chrome_driver._terminate_matching_processes("chrome") == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")
@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep not available")
def test_terminate_matching_processes_spares_its_own_grandparent(
    tmp_path: Any,
) -> None:
    """End-to-end without mocks: the grandparent survives, a stranger dies.

    Reproduction of the measured case, at its real depth. Only the *grandparent*
    carries the marker in its argv: pytest -> CLI subprocess -> cleanup. The
    grandchild runs the cleanup for that very marker, and before the fix this was
    fatal (measured as exit 143 on a pytest run).

    The depth matters. A two-level version (parent -> child) would pass even with
    the whole ancestry walk deleted, because ``_protected_pids`` seeds itself with
    ``os.getppid()``; only from the grandparent upwards does the walk carry the
    result. Verified by mutation: removing the walk keeps a two-level test green
    and turns this one red.

    A ``stranger`` with the same marker proves the cleanup still terminates
    unrelated processes: the fix must be a filter, not an off switch. A unique
    UUID marker is used instead of ``chrome`` so nothing else on the machine can
    be hit.
    """

    marker = f"gfmy-kill-probe-{uuid.uuid4().hex}"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Level 3: runs the cleanup. Its argv does NOT carry the marker.
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "from custom_components.googlefindmy import chrome_driver\n"
        "print(chrome_driver._terminate_matching_processes(sys.argv[1]))\n",
        encoding="utf-8",
    )
    # Level 2: pure relay, marker-free argv, so the marked process is strictly
    # the grandparent of the cleanup.
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "proc = subprocess.run([sys.executable, sys.argv[1], sys.argv[2]],\n"
        "                      capture_output=True, text=True, timeout=60)\n"
        "sys.stdout.write(proc.stdout)\n"
        "sys.stderr.write(proc.stderr)\n"
        "sys.exit(proc.returncode)\n",
        encoding="utf-8",
    )
    # Level 1: the only process whose command line matches the marker.
    grandparent = tmp_path / "grandparent.py"
    grandparent.write_text(
        "import subprocess, sys\n"
        "proc = subprocess.run(\n"
        "    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3]],\n"
        "    capture_output=True, text=True, timeout=60)\n"
        "sys.stdout.write(proc.stdout)\n"
        "sys.stderr.write(proc.stderr)\n"
        "sys.exit(proc.returncode)\n",
        encoding="utf-8",
    )

    sleeper = "import sys, time; time.sleep(30)"
    stranger = subprocess.Popen(  # noqa: S603 - fixed interpreter, generated marker
        [sys.executable, "-c", sleeper, marker],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Only the grandparent's argv carries the marker, so pgrep returns it and
        # nothing but the ancestry walk keeps it alive.
        result = subprocess.run(  # noqa: S603 - fixed interpreter, generated files
            [sys.executable, str(grandparent), str(parent), str(child), marker],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        assert result.returncode == 0, (
            "the grandparent was killed by its own grandchild: "
            f"rc={result.returncode} stderr={result.stderr}"
        )
        assert int(result.stdout.strip() or 0) >= 1, (
            f"the stranger should have been signalled, output was {result.stdout!r}"
        )
        assert stranger.wait(timeout=10) != 0
    finally:
        if stranger.poll() is None:  # pragma: no cover - cleanup path
            stranger.kill()
            stranger.wait(timeout=10)


# ---------------------------------------------------------------------------
# _is_container_login / container pre-kill + teardown guard (regression)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("0", False), ("", False), (None, False)],
)
def test_is_container_login_reads_env(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    """The container signal is the exact string ``"1"`` in the shared env var."""

    if value is None:
        monkeypatch.delenv("GOOGLEFINDMY_CONTAINER_LOGIN", raising=False)
    else:
        monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", value)

    assert chrome_driver._is_container_login() is expected


def test_kill_existing_chrome_processes_skipped_in_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: inside the docker-login container the broad Chrome pre-kill
    must be skipped.

    In the ``selenium/standalone-chrome`` base image matching ``chrome`` against
    the full command line hits the Java Selenium Grid node, and its exit makes
    supervisord tear the whole stack down (crash log:
    ``selenium-standalone (exit status 143)`` -> ``received SIGINT`` ->
    noVNC/vnc/xvfb stopped). Without the guard this test fails because
    ``subprocess.run`` is invoked.

    The ancestry filter added later does not replace this guard: the Grid node is
    a sibling under supervisord, not an ancestor, so it would not be spared.
    """

    monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("no process lookup may run inside the container"),
    )

    # Must be a no-op on the kill path (no subprocess call, no raise).
    chrome_driver._kill_existing_chrome_processes()


def test_safe_quit_driver_skips_force_kill_in_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the teardown chromedriver force-kill is skipped in the
    container too, so it cannot collapse the shared Selenium/noVNC stack; the
    driver is still quit normally.
    """

    monkeypatch.setenv("GOOGLEFINDMY_CONTAINER_LOGIN", "1")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail(
            "chromedriver force-kill must not run inside the container"
        ),
    )

    quit_calls = {"n": 0}

    class _Driver:
        def quit(self) -> None:
            quit_calls["n"] += 1

    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]

    assert quit_calls["n"] == 1


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
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    calls, kills = _stub_pgrep(monkeypatch, [4243])

    quit_calls = {"n": 0}

    class _Driver:
        def quit(self) -> None:
            quit_calls["n"] += 1

    chrome_driver.safe_quit_driver(_Driver())  # type: ignore[arg-type]

    assert quit_calls["n"] == 1
    assert calls == [["pgrep", "-f", "chromedriver"]]
    assert kills == [(4243, signal.SIGTERM)]


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

    def fake_chrome(
        *, options: Any, version_main: int | None = None, **k: object
    ) -> object:
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

    def fake_chrome(
        *, options: Any, version_main: int | None = None, **k: object
    ) -> object:
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
    # ``**_`` rather than the signature of the day: this test cares that the
    # fallback is reached, not how it is parameterised. Pinning the keywords
    # here made a later, unrelated parameter surface as a TypeError raised from
    # inside the code under test -- a failure of the double reported as a
    # failure of the chain.
    monkeypatch.setattr(
        chrome_driver,
        "_try_webdriver_manager_fallback",
        lambda **_: fallback_driver,
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
        lambda *, options, version_main=None, **k: (
            captured.append(version_main) or object()
        ),
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


class _FakeCapsDriver:
    """Minimal WebDriver stand-in exposing a ``capabilities`` mapping."""

    def __init__(self, capabilities: object) -> None:
        self.capabilities = capabilities

    def quit(self) -> None:  # pragma: no cover - success path never quits
        """No-op quit for API compatibility."""


def test_version_guard_warns_on_driver_chrome_major_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live session whose ChromeDriver major differs from Chrome logs a warning."""

    driver = _FakeCapsDriver(
        {
            "browserVersion": "150.0.7258.66",
            "chrome": {"chromedriverVersion": "151.0.7300.0 (abcdef)"},
        }
    )
    with caplog.at_level(logging.WARNING, logger=chrome_driver.LOGGER.name):
        chrome_driver._warn_on_driver_version_mismatch(driver, detected_version=150)

    assert any(
        "does not match the running Chrome major" in record.getMessage()
        for record in caplog.records
    )


def test_version_guard_silent_when_majors_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chrome 150 with ChromeDriver 150 emits no warning (the good case)."""

    driver = _FakeCapsDriver(
        {
            "browserVersion": "150.0.7258.66",
            "chrome": {"chromedriverVersion": "150.0.7258.66 (abcdef)"},
        }
    )
    with caplog.at_level(logging.WARNING, logger=chrome_driver.LOGGER.name):
        chrome_driver._warn_on_driver_version_mismatch(driver, detected_version=150)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_version_guard_defensive_on_incomplete_capabilities(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing capability keys degrade to a debug log and never raise or warn."""

    driver = _FakeCapsDriver({"browserVersion": "150.0.7258.66"})  # no chrome key
    with caplog.at_level(logging.WARNING, logger=chrome_driver.LOGGER.name):
        chrome_driver._warn_on_driver_version_mismatch(driver, detected_version=None)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("150.0.7258.66", 150),
        ("150.0.7258.66 (abcdef)", 150),
        ("not-a-version", None),
        ("", None),
        (150, None),  # not a string
    ],
)
def test_major_from_version_string_parses_or_degrades(
    value: object, expected: int | None
) -> None:
    """Unparseable capability strings degrade to ``None`` instead of raising.

    Pre-existing gap picked up in the open diff (code-architekt rule 20): the
    ``ValueError`` branch had no test, and covering it costs one parametrisation.
    """

    assert chrome_driver._major_from_version_string(value) == expected


def test_version_guard_debug_logs_a_stale_detected_major(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Driver and session agree, but the earlier detection disagreed: debug only."""

    driver = _FakeCapsDriver(
        {
            "browserVersion": "150.0.7258.66",
            "chrome": {"chromedriverVersion": "150.0.7258.66 (abcdef)"},
        }
    )
    with caplog.at_level(logging.DEBUG, logger=chrome_driver.LOGGER.name):
        chrome_driver._warn_on_driver_version_mismatch(driver, detected_version=149)

    assert any(
        "differs from the live session's Chrome" in record.getMessage()
        for record in caplog.records
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_version_guard_never_raises_on_a_hostile_driver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A capabilities property that raises must not break driver creation."""

    class _ExplodingDriver:
        @property
        def capabilities(self) -> dict[str, Any]:
            raise RuntimeError("capabilities unavailable")

    with caplog.at_level(logging.DEBUG, logger=chrome_driver.LOGGER.name):
        chrome_driver._warn_on_driver_version_mismatch(
            _ExplodingDriver(),  # type: ignore[arg-type]
            detected_version=150,
        )

    assert any(
        "Post-construction version guard skipped" in record.getMessage()
        for record in caplog.records
    )


def test_create_driver_warns_but_returns_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A successful driver with a mismatched major is warned about, not rejected.

    Regression for the post-construction version guard: Chrome 150 with a
    ChromeDriver 151 must still return the working driver (the runtime guard is
    non-fatal) while surfacing the actionable warning.
    """

    fake_driver = _FakeCapsDriver(
        {
            "browserVersion": "150.0.7258.66",
            "chrome": {"chromedriverVersion": "151.0.7300.0 (abcdef)"},
        }
    )
    monkeypatch.setattr(chrome_driver, "_kill_existing_chrome_processes", lambda: None)
    monkeypatch.setattr(chrome_driver, "find_chrome", lambda: "/opt/chrome")
    monkeypatch.setattr(chrome_driver, "get_chrome_version", lambda _p, **_k: 150)
    monkeypatch.delenv("GOOGLEFINDMY_CHROME_PATH", raising=False)
    monkeypatch.delenv("GOOGLEFINDMY_CHROME_VERSION", raising=False)

    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setattr(
        uc_module, "Chrome", lambda *, options, version_main=None, **k: fake_driver
    )

    with caplog.at_level(logging.WARNING, logger=chrome_driver.LOGGER.name):
        result = chrome_driver.create_driver(headless=True)

    assert result is fake_driver
    assert any(
        "does not match the running Chrome major" in record.getMessage()
        for record in caplog.records
    )


# --- Login language ------------------------------------------------------------
#
# The container login used to show Google's sign-in page in English no matter who
# was looking at it, because Chrome inherits no language in a bare image. The
# variable below lets a user hand their own language in. The tests pin the two
# halves of the promise: opting in localises both the browser and the request,
# and staying silent changes nothing at all.


def test_login_locale_adds_both_language_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.setenv(chrome_driver.ENV_LOGIN_LOCALE, "de-DE")

    options = chrome_driver.get_options(headless=True)

    # --lang localises Chrome, --accept-lang the page Google serves. One without
    # the other leaves the user with half a translation.
    assert "--lang=de-DE" in options.arguments
    assert "--accept-lang=de-DE" in options.arguments


def test_no_locale_leaves_chrome_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must not choose a language for anyone."""
    uc_module = chrome_driver._get_uc_module()
    monkeypatch.setattr(uc_module, "ChromeOptions", FakeChromeOptions)
    monkeypatch.delenv(chrome_driver.ENV_LOGIN_LOCALE, raising=False)

    options = chrome_driver.get_options(headless=True)

    assert not [arg for arg in options.arguments if "lang" in arg]


def _fallback_options_with_env(
    monkeypatch: pytest.MonkeyPatch,
    locale: str | None,
    *,
    headless: bool = False,
    resolved_path: str | None = None,
) -> FakeChromeOptions:
    """Run the webdriver-manager fallback and return the options it built.

    Returns the object, not just its argument list, because not every caller
    preference arrives as an argument: the Chrome binary is an attribute.

    The fallback is strategy 5: it is reached only after the four
    undetected-chromedriver attempts have failed, which is exactly when nobody
    is watching closely enough to notice that the sign-in page came back in the
    wrong language.
    """
    built: list[FakeChromeOptions] = []

    def _make_options() -> FakeChromeOptions:
        options = FakeChromeOptions()
        built.append(options)
        return options

    fake_driver = object()

    def _make_chrome(*, service: object, options: object) -> object:
        # The arguments are read off built[0] further down; pin that this is the
        # object Chrome actually received, so a builder that constructs two and
        # passes the wrong one cannot pass by being measured on the right one.
        assert options is built[0]
        return fake_driver

    monkeypatch.setattr(chrome_driver, "_WEBDRIVER_MANAGER_AVAILABLE", True)
    monkeypatch.setattr(
        chrome_driver,
        "_selenium_webdriver",
        SimpleNamespace(ChromeOptions=_make_options, Chrome=_make_chrome),
    )
    monkeypatch.setattr(
        chrome_driver, "_chrome_service_cls", lambda path: SimpleNamespace(path=path)
    )
    monkeypatch.setattr(
        chrome_driver,
        "_chrome_driver_manager_cls",
        lambda: SimpleNamespace(install=lambda: "/driver"),
    )
    if locale is None:
        monkeypatch.delenv(chrome_driver.ENV_LOGIN_LOCALE, raising=False)
    else:
        monkeypatch.setenv(chrome_driver.ENV_LOGIN_LOCALE, locale)

    assert (
        chrome_driver._try_webdriver_manager_fallback(
            headless=headless, resolved_path=resolved_path
        )
        is fake_driver
    )
    assert len(built) == 1
    return built[0]


def test_webdriver_manager_fallback_honours_the_login_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strategy 5 builds its own options; the language must survive that.

    Regression for the Codex finding on PR #1261, reviewed at commit 9b20e736
    and introduced at 9d0a2841: the language was added inside get_options(), so
    the one path that does not call it silently ignored the variable and served
    the container default instead.
    """
    arguments = _fallback_options_with_env(monkeypatch, "pt-BR").arguments

    assert "--lang=pt-BR" in arguments
    assert "--accept-lang=pt-BR" in arguments


def test_webdriver_manager_fallback_stays_silent_without_a_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting out has to mean the same thing on every path, too.

    Not a regression test: this one is green before the fix as well, because a
    path that ignored the variable ignored it in both directions. It is here to
    stop the fix from over-applying, i.e. from inventing a default language for
    someone who never asked for one.
    """
    arguments = _fallback_options_with_env(monkeypatch, None).arguments

    assert not [arg for arg in arguments if "lang" in arg]


def test_webdriver_manager_fallback_rejects_an_invalid_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validation has to hold on this path too, not just by composition.

    The value is spliced into a Chrome command line. That it is validated is
    covered for the helper in isolation; this pins that the fallback really goes
    through the validating helper and cannot be handed a second switch.
    """
    options = _fallback_options_with_env(monkeypatch, "de-DE --no-sandbox")
    arguments = options.arguments

    assert not [arg for arg in arguments if "lang" in arg]
    # Not "--no-sandbox is present": that holds under a successful injection
    # too, because the smuggled switch would ride inside a *different* element.
    # What rules injection out is that no element carries a second switch.
    assert not [arg for arg in arguments if " " in arg], arguments


@pytest.mark.parametrize("headless", [False, True])
def test_webdriver_manager_fallback_follows_the_requested_window_mode(
    monkeypatch: pytest.MonkeyPatch, headless: bool
) -> None:
    """Same finding class as the locale, same answer.

    create_driver(headless=True) reaches strategy 5, and a fallback that opens a
    visible window fails exactly in the environment that asked for headless,
    i.e. one without a display.
    """
    arguments = _fallback_options_with_env(
        monkeypatch, None, headless=headless
    ).arguments

    assert ("--headless" in arguments) is headless
    assert ("--start-maximized" in arguments) is not headless
    # --disable-gpu belongs to the headless mode, not to the fallback: it is
    # what get_options() pairs with --headless unconditionally, and headless
    # without it is the documented failure combination on older builds and on
    # Windows. Asserted rather than merely executed, so that dropping it turns
    # a test red instead of only changing a coverage number.
    assert ("--disable-gpu" in arguments) is headless


@pytest.mark.parametrize("resolved_path", [None, "/opt/portable/chrome"])
def test_webdriver_manager_fallback_uses_the_resolved_chrome_binary(
    monkeypatch: pytest.MonkeyPatch, resolved_path: str | None
) -> None:
    """Same finding class as the locale, third instance.

    The four earlier strategies all point Chrome at GOOGLEFINDMY_CHROME_PATH.
    A fallback that does not either starts a different browser than the user
    chose, or fails outright on a machine whose Chrome is not on PATH -- and it
    runs precisely when the other four have already failed, so there is nothing
    behind it to correct the mistake.
    """
    options = _fallback_options_with_env(monkeypatch, None, resolved_path=resolved_path)

    assert options.binary_location == resolved_path


def test_every_chrome_options_builder_applies_the_login_locale() -> None:
    """The guard against the finding coming back through a third builder.

    The tests above pin the two builders that exist today. This one pins the
    rule, by reading the module's own syntax tree: every function that
    constructs ChromeOptions must also call _apply_login_locale. A future
    builder that inlines the switches again, or forgets them, is caught even
    though no test executes it -- which a test that merely counts calls into the
    two known builders could never do.
    """
    tree = ast.parse(pathlib.Path(chrome_driver.__file__).read_text(encoding="utf-8"))

    def _calls(node: ast.AST) -> set[str]:
        names: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
        return names

    builders = {
        node.name: _calls(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and "ChromeOptions" in _calls(node)
    }

    # Griff-Kontrolle, in both directions. Non-emptiness alone would still pass
    # after a builder is renamed out of view, which is the failure this test
    # exists to notice: it would then report success having lost exactly the
    # path it was written for. Naming the two known builders costs an update
    # when one is renamed on purpose -- and that update is the point.
    assert {"get_options", "_try_webdriver_manager_fallback"} <= builders.keys(), (
        f"the guard lost its grip: it sees {sorted(builders)}. If a builder was "
        f"renamed, rename it here too; if one was removed, say so here."
    )

    missing = sorted(
        name for name, calls in builders.items() if "_apply_login_locale" not in calls
    )
    assert not missing, (
        f"these builders construct ChromeOptions without applying the user's "
        f"language: {missing}. Call _apply_login_locale(options, os.environ)."
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("de", "de"),
        ("pt-BR", "pt-BR"),
        # What a shell actually hands out. Accepted and translated rather than
        # rejected, because this is the value users will copy from `echo $LANG`.
        ("de_DE.UTF-8", "de-DE"),
        ("pt_BR@euro", "pt-BR"),
        ("  fr-CA  ", "fr-CA"),
    ],
)
def test_login_locale_accepts_tags_and_posix_locales(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(chrome_driver.ENV_LOGIN_LOCALE, raw)
    assert chrome_driver._login_locale(os.environ) == expected


@pytest.mark.parametrize(
    ("raw", "expected", "normalised"),
    [
        # Dropped-and-accepted: the suffix is never inspected, so this is the
        # class the README now names instead of promising a warning for it.
        ("de-DE.anything", "de-DE", True),
        ("de_DE.UTF-8", "de-DE", True),
        # Unchanged input must stay silent: a debug line per call would bury the
        # one case worth reading.
        ("pt-BR", "pt-BR", False),
    ],
)
def test_a_shortened_locale_leaves_a_trace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: str,
    expected: str,
    normalised: bool,
) -> None:
    """Silent shortening is the half a user cannot otherwise diagnose.

    A rejected value announces itself at warning level. A value that was merely
    cut short still reaches Chrome, and the surviving tag is already logged by
    ``_apply_login_locale``; what was missing is the pairing with what the user
    actually set. Kept at debug because it is a trace, not a problem -- the level
    is asserted, or the mutation to ``warning`` would pass unnoticed.
    """
    monkeypatch.setenv(chrome_driver.ENV_LOGIN_LOCALE, raw)

    with caplog.at_level(logging.DEBUG, logger=chrome_driver.LOGGER.name):
        assert chrome_driver._login_locale(os.environ) == expected

    said_so = [r for r in caplog.records if "Normalised" in r.getMessage()]
    assert bool(said_so) is normalised, (
        f"expected normalisation trace={normalised} for {raw!r}: {caplog.text}"
    )
    if normalised:
        assert said_so[0].levelno == logging.DEBUG, (
            "the trace must stay at debug: a shortened value is not a problem, "
            "and a warning here would fire on every well-formed POSIX locale."
        )
        # The order carries the meaning: raw -> normalised. Asserting only that
        # both appear lets the two %r arguments be swapped, which turns the
        # trace into a claim that the user set the short form and got the long
        # one -- worse than no line at all.
        rendered = said_so[0].getMessage()
        assert rendered.index(repr(raw)) < rendered.index(repr(expected)), rendered
        # The variable name is the half a user greps for; without it the trace
        # cannot be found by the person the README sends looking for it.
        assert chrome_driver.ENV_LOGIN_LOCALE in rendered, rendered


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "German",  # a language name, not a tag
        "de-DE --no-sandbox",  # the reason this is validated at all
        "../../etc/passwd",
        "de;rm -rf /",
    ],
)
def test_a_bad_locale_is_ignored_not_fatal(
    monkeypatch: pytest.MonkeyPatch, raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A cosmetic preference must never be able to stop or steer the login.

    The two whitespace-carrying values are the point: the value is spliced into
    a Chrome command line, so anything that could smuggle a second switch has to
    be refused rather than passed through.
    """
    monkeypatch.setenv(chrome_driver.ENV_LOGIN_LOCALE, raw)

    with caplog.at_level(logging.WARNING):
        assert chrome_driver._login_locale(os.environ) is None

    if raw.strip():
        assert chrome_driver.ENV_LOGIN_LOCALE in caplog.text

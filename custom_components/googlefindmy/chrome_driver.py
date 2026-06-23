from __future__ import annotations

import contextlib
import importlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.remote.webdriver import WebDriver as RemoteWebDriver

# Platform-specific import for Windows registry access
_winreg: ModuleType | None = None
if sys.platform == "win32":
    import winreg as _winreg

# Optional imports for webdriver-manager fallback
_selenium_webdriver: Any = None
_chrome_service_cls: Any = None
_chrome_driver_manager_cls: Any = None
_WEBDRIVER_MANAGER_AVAILABLE = False

try:
    from selenium import webdriver as _selenium_webdriver_module
    from selenium.webdriver.chrome.service import Service as _ChromeServiceClass
    from webdriver_manager.chrome import (
        ChromeDriverManager as _ChromeDriverManagerClass,
    )

    _selenium_webdriver = _selenium_webdriver_module
    _chrome_service_cls = _ChromeServiceClass
    _chrome_driver_manager_cls = _ChromeDriverManagerClass
    _WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    pass

LOGGER = logging.getLogger(__name__)

# Environment variable overrides for the layered resolution chain
# (priority: CLI argument > environment variable > auto-detection).
_ENV_CHROME_PATH = "GOOGLEFINDMY_CHROME_PATH"
_ENV_CHROME_VERSION = "GOOGLEFINDMY_CHROME_VERSION"


def _load_uc() -> Any:
    """Import undetected-chromedriver with a stub fallback.

    GitHub runners remove ``distutils`` from the standard library, which breaks
    ``undetected_chromedriver`` imports. Rather than failing at module import
    time, fall back to a lightweight stub that raises a descriptive error when
    used. Tests can monkeypatch the stub as needed.
    """

    try:
        return importlib.import_module("undetected_chromedriver")
    except ImportError as err:
        LOGGER.debug(
            "undetected_chromedriver is unavailable; falling back to stub: %s", err
        )
        error = err

        class _StubChromeOptions:
            def __init__(self) -> None:
                self.arguments: list[str] = []
                self.binary_location: str | None = None

            def add_argument(self, argument: str) -> None:
                self.arguments.append(argument)

        def _stub_chrome(*, options: object) -> WebDriver:
            raise RuntimeError(
                "undetected_chromedriver could not be imported; install its runtime "
                "dependencies (including setuptools' distutils module)"
            ) from error

        return SimpleNamespace(ChromeOptions=_StubChromeOptions, Chrome=_stub_chrome)


_UC_CACHE = SimpleNamespace(module=None)


def _get_uc_module() -> Any:
    """Lazily import and cache ``undetected_chromedriver``."""

    if _UC_CACHE.module is None:
        _UC_CACHE.module = cast(Any, _load_uc())

    return _UC_CACHE.module


def _reset_uc_cache(module: Any | None = None) -> None:
    """Reset the cached ``undetected_chromedriver`` module.

    This helper exists for tests that need to inject a stub without
    importing the real dependency.
    """

    _UC_CACHE.module = module


type ChromeOptions = Any


def get_chrome_version(chrome_path: str) -> int | None:
    """Get Chrome version from executable.

    Parameters
    ----------
    chrome_path: str
        Path to the Chrome executable.

    Returns
    -------
    int | None
        The major Chrome version number, or None if it could not be determined.
    """
    try:
        if platform.system() == "Windows":
            # Try to get version from registry first
            if _winreg is not None:
                try:
                    key = _winreg.OpenKey(
                        _winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"
                    )
                    version, _ = _winreg.QueryValueEx(key, "version")
                    _winreg.CloseKey(key)
                    return int(version.split(".")[0])
                except Exception:  # noqa: BLE001 - defensive fallback
                    pass
            # Fallback: run chrome with --version
            result = subprocess.run(
                [chrome_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version_match = re.search(r"(\d+)\.\d+\.\d+\.\d+", result.stdout)
            if version_match:
                return int(version_match.group(1))
        else:
            result = subprocess.run(
                [chrome_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version_match = re.search(r"(\d+)\.\d+\.\d+\.\d+", result.stdout)
            if version_match:
                return int(version_match.group(1))
    except Exception as err:  # noqa: BLE001 - defensive logging
        LOGGER.debug("Could not determine Chrome version: %s", err)
    return None


def _kill_existing_chrome_processes() -> None:
    """Terminate any existing Chrome processes to avoid conflicts.

    This helps prevent issues when Chrome is already running or has zombie processes.
    """
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/f", "/im", "chrome.exe"],
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(["pkill", "-f", "chrome"], capture_output=True, check=False)
        time.sleep(2)  # Allow time for processes to terminate
    except Exception:  # pragma: no cover - defensive, best-effort cleanup
        LOGGER.debug("Failed to kill existing Chrome processes (non-fatal)")


def find_chrome() -> str | None:
    """Locate the Chrome executable on the current system.

    Returns
    -------
    str | None
        The absolute path to the Chrome binary if it could be resolved, otherwise ``None``.
    """
    # Expand %USERNAME% for Windows paths
    username = os.environ.get("USERNAME", os.environ.get("USER", ""))

    possible_paths = [
        # Windows paths
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\ProgramData\chocolatey\bin\chrome.exe",
        os.path.expandvars(
            r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe"
        ),
        f"C:\\Users\\{username}\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe",
        # Additional Windows paths for Chrome installed per-user
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        os.path.join(
            os.environ.get("PROGRAMFILES(X86)", ""),
            "Google",
            "Chrome",
            "Application",
            "chrome.exe",
        ),
        # Linux paths
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/local/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        # macOS paths
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        ),
    ]

    # Filter out empty paths and check for existence
    for path in possible_paths:
        if path and os.path.exists(path):
            LOGGER.debug("Found Chrome at: %s", path)
            return path

    # Use system command to find Chrome
    try:
        if platform.system() == "Windows":
            # Try multiple executable names on Windows
            for name in ["chrome", "google-chrome", "chromium"]:
                chrome_path = shutil.which(name)
                if chrome_path:
                    return chrome_path
            # Try using 'where' command on Windows
            try:
                result = subprocess.run(
                    ["where", "chrome.exe"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip().split("\n")[0]
            except Exception:  # noqa: BLE001 - defensive fallback
                pass
        else:
            for name in [
                "google-chrome",
                "google-chrome-stable",
                "chromium",
                "chromium-browser",
            ]:
                chrome_path = shutil.which(name)
                if chrome_path:
                    return chrome_path
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("Failed to resolve Chrome binary via PATH lookup")

    return None


def get_options(*, headless: bool = False) -> ChromeOptions:
    """Create Chrome options that match the integration's requirements.

    Parameters
    ----------
    headless: bool
        Whether the browser should run in headless mode.

    Returns
    -------
    undetected_chromedriver.ChromeOptions
        The configured Chrome options instance.
    """

    chrome_options = _get_uc_module().ChromeOptions()
    if not headless:
        chrome_options.add_argument("--start-maximized")
    else:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-web-security")
    chrome_options.add_argument("--allow-running-insecure-content")

    return chrome_options


def get_driver(
    chrome_path: str | None,
    *,
    chrome_version: int | None = None,
    headless: bool = False,
) -> WebDriver:
    """Initialize and return an undetected Chrome driver.

    Parameters
    ----------
    chrome_path: str | None
        Path to the Chrome executable.
    chrome_version: int | None
        Optional explicit Chrome major version override (highest priority).
    headless: bool
        Whether to run the browser in headless mode.

    Returns
    -------
    WebDriver
        Configured Chrome WebDriver instance.
    """

    options = get_options(headless=headless)
    if chrome_path:
        options.binary_location = chrome_path

    # Apply the same resolution invariant as the main path: never escalate to
    # version_main=None while a version is known (explicit/env/detected).
    detected = get_chrome_version(chrome_path) if chrome_path else None
    resolved_version, _source = _resolve_chrome_version(
        explicit=chrome_version,
        env_raw=os.environ.get(_ENV_CHROME_VERSION),
        detected=detected,
    )

    return cast(
        WebDriver,
        _get_uc_module().Chrome(options=options, version_main=resolved_version),
    )


def _try_webdriver_manager_fallback() -> WebDriver | None:
    """Try to use webdriver-manager as a fallback for standard Selenium.

    Returns
    -------
    WebDriver | None
        A WebDriver instance if successful, otherwise None.
    """
    if not _WEBDRIVER_MANAGER_AVAILABLE:
        LOGGER.debug("webdriver-manager not available, skipping fallback")
        return None

    driver: WebDriver | None = None
    try:
        LOGGER.info("Attempting webdriver-manager fallback...")
        service = _chrome_service_cls(_chrome_driver_manager_cls().install())
        options = _selenium_webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = _selenium_webdriver.Chrome(service=service, options=options)
        LOGGER.warning(
            "Started using webdriver-manager (standard Selenium). "
            "This uses standard Selenium without bot detection bypass!"
        )
        return driver
    except Exception as err:  # noqa: BLE001 - fallback should not raise
        _quit_driver(driver)
        LOGGER.debug("webdriver-manager fallback failed: %s", err)
        return None


def safe_quit_driver(driver: RemoteWebDriver | None) -> None:
    """Safely quit the Chrome driver, handling WinError 6 and other errors.

    Parameters
    ----------
    driver: RemoteWebDriver | None
        The WebDriver instance to quit, or None.
    """
    if driver is None:
        return

    try:
        # Try normal quit first
        driver.quit()
    except OSError as err:
        # Handle "WinError 6: The handle is invalid" and similar errors
        LOGGER.debug("OSError during driver quit (usually harmless): %s", err)
    except Exception as err:  # noqa: BLE001 - cleanup should not raise
        LOGGER.debug("Error during driver quit: %s", err)
    finally:
        # Force kill any remaining processes
        try:
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/f", "/im", "chromedriver.exe"],
                    capture_output=True,
                    check=False,
                )
            else:
                subprocess.run(
                    ["pkill", "-f", "chromedriver"],
                    capture_output=True,
                    check=False,
                )
        except Exception:  # noqa: BLE001 - cleanup should not raise
            pass


def _quit_driver(driver: WebDriver | None) -> None:
    """Silently quit a partially-initialized driver to avoid leftover Chrome windows."""
    if driver is not None:
        with contextlib.suppress(Exception):
            driver.quit()


def create_driver(
    chrome_path: str | None = None,
    *,
    chrome_version: int | None = None,
    headless: bool = False,
) -> WebDriver:
    """Backward-compatible wrapper for driver creation with multiple fallbacks.

    Attempts driver creation in this order:
    1. Standard creation with version_main for Chrome version compatibility
    2. Fallback with explicit Chrome path from system
    3. Fallback without specifying version
    4. Headless mode
    5. webdriver-manager fallback (standard Selenium)

    On Windows, ``undetected_chromedriver`` may fail with ``WinError 32``
    (file in use) if a previous ChromeDriver process hasn't fully released
    its lock on ``chromedriver.exe``.  This function retries up to 3 times
    with a short delay to handle transient file locks.
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return _create_driver_inner(
                chrome_path=chrome_path,
                chrome_version=chrome_version,
                headless=headless,
            )
        except (PermissionError, OSError) as err:
            # WinError 32 (file in use) or WinError 183 (file already exists)
            err_str = str(err)
            is_file_lock = "WinError 32" in err_str or "WinError 183" in err_str
            if not is_file_lock or attempt >= max_retries - 1:
                raise
            LOGGER.info(
                "ChromeDriver file lock detected (attempt %d/3), "
                "waiting for lock release...",
                attempt + 1,
            )
            time.sleep(3)
        except RuntimeError:
            if attempt >= max_retries - 1 or platform.system() != "Windows":
                raise
            # All strategies failed — on Windows this may be due to file locks;
            # retry after a delay.
            LOGGER.info(
                "All ChromeDriver strategies failed (attempt %d/3), "
                "retrying after delay...",
                attempt + 1,
            )
            time.sleep(3)

    # Should not reach here, but satisfy type checker
    return _create_driver_inner(
        chrome_path=chrome_path, chrome_version=chrome_version, headless=headless
    )


def _is_file_lock_error(err: BaseException) -> bool:
    """Check if an exception is a Windows file lock error (WinError 32/183)."""
    msg = str(err)
    return "WinError 32" in msg or "WinError 183" in msg


def _is_version_mismatch_error(err: BaseException) -> bool:
    """Check if an exception looks like a Chrome/driver version mismatch.

    undetected-chromedriver surfaces Selenium's "only supports Chrome version N"
    message when the driver and the installed browser disagree on the version.
    """
    return "only supports Chrome version" in str(err)


def _log_version_mismatch_hint(
    error: BaseException | None,
    *,
    detected_version: int | None,
    resolved_version: int | None,
    version_source: str,
) -> None:
    """Log an actionable hint when the last failure was a version mismatch.

    Replaces Selenium's bare "only supports Chrome version" line with the
    detected/used versions, the override source and how to pin a version.
    """
    if error is None or not _is_version_mismatch_error(error):
        return
    LOGGER.error(
        "Chrome/driver version mismatch (detected: %s, requested/used: %s, "
        "source: %s). Pin a matching major version via --chrome-version or the "
        "%s environment variable (set it to your installed Chrome major "
        "version).",
        detected_version or "Unknown",
        resolved_version or "Unknown",
        version_source,
        _ENV_CHROME_VERSION,
    )


def _parse_env_version(env_raw: str | None) -> int | None:
    """Parse a ``GOOGLEFINDMY_CHROME_VERSION`` value into a major version int.

    An unset, empty or whitespace-only value yields ``None``. A non-integer
    value is ignored with a warning (so resolution falls through to the next
    lower-priority source) rather than raising.
    """
    if env_raw is None:
        return None
    stripped = env_raw.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        LOGGER.warning(
            "Ignoring invalid %s=%r (not an integer); "
            "falling back to lower-priority source",
            _ENV_CHROME_VERSION,
            env_raw,
        )
        return None


def _resolve_chrome_version(
    *, explicit: int | None, env_raw: str | None, detected: int | None
) -> tuple[int | None, str]:
    """Resolve the effective Chrome major version and its provenance.

    Priority: ``explicit`` (CLI) > environment variable > ``detected`` (auto).
    Returns ``(version, source)`` where ``source`` is one of ``"cli"``,
    ``"env"``, ``"auto"`` or ``"none"``. An invalid/empty env value is skipped
    (see :func:`_parse_env_version`).
    """
    if explicit is not None:
        return explicit, "cli"
    parsed_env = _parse_env_version(env_raw)
    if parsed_env is not None:
        return parsed_env, "env"
    if detected is not None:
        return detected, "auto"
    return None, "none"


def _resolve_chrome_path(
    *, explicit: str | None, env_raw: str | None, detected: str | None
) -> tuple[str | None, str]:
    """Resolve the effective Chrome binary path and its provenance.

    Priority: ``explicit`` (CLI) > environment variable > ``detected`` (auto).
    An empty/whitespace-only env value is treated as unset. The three sources
    are kept separate on purpose: collapsing ``explicit or detected`` before
    consulting the env var would make the env unreachable between CLI and auto.
    Returns ``(path, source)``.
    """
    if explicit:
        return explicit, "cli"
    if env_raw and env_raw.strip():
        return env_raw.strip(), "env"
    if detected:
        return detected, "auto"
    return None, "none"


def _try_strategy_default(
    *, resolved_path: str | None, version_main: int | None, headless: bool
) -> tuple[WebDriver | None, BaseException | None]:
    """Strategy 1: default creation, passing ``version_main`` if detected.

    Returns ``(driver, error)``. ``driver`` is ``None`` on failure and ``error``
    is the caught exception (``None`` on success), letting the caller track
    ``all_file_lock`` and surface a version-mismatch hint on total failure.
    """
    driver: WebDriver | None = None
    try:
        options = get_options(headless=headless)
        if resolved_path:
            options.binary_location = resolved_path
        driver = cast(
            WebDriver,
            _get_uc_module().Chrome(options=options, version_main=version_main),
        )
        LOGGER.debug("ChromeDriver started successfully.")
        return driver, None
    except Exception as err:  # noqa: BLE001
        _quit_driver(driver)
        LOGGER.warning("Strategy 1 (default) failed: %s", err)
        return None, err


def _try_strategy_explicit_path(
    *, resolved_path: str, version_main: int | None, headless: bool
) -> tuple[WebDriver | None, BaseException | None]:
    """Strategy 2: pass ``browser_executable_path`` explicitly.

    See :func:`_try_strategy_default` for the return contract.
    """
    driver: WebDriver | None = None
    try:
        options = get_options(headless=headless)
        driver = cast(
            WebDriver,
            _get_uc_module().Chrome(
                options=options,
                version_main=version_main,
                browser_executable_path=resolved_path,
            ),
        )
        LOGGER.debug("ChromeDriver started with browser_executable_path.")
        return driver, None
    except Exception as err:  # noqa: BLE001
        _quit_driver(driver)
        LOGGER.warning("Strategy 2 (explicit path) failed: %s", err)
        return None, err


def _try_strategy_no_version(
    *, resolved_path: str | None, headless: bool
) -> tuple[WebDriver | None, BaseException | None]:
    """Strategy 3: attempt creation without specifying a version.

    See :func:`_try_strategy_default` for the return contract.
    """
    driver: WebDriver | None = None
    try:
        options = get_options(headless=headless)
        if resolved_path:
            options.binary_location = resolved_path
        driver = cast(
            WebDriver, _get_uc_module().Chrome(options=options, version_main=None)
        )
        LOGGER.debug("ChromeDriver started without explicit version.")
        return driver, None
    except Exception as err:  # noqa: BLE001
        _quit_driver(driver)
        LOGGER.warning("Strategy 3 (no version) failed: %s", err)
        return None, err


def _try_strategy_headless(
    *, resolved_path: str | None, version_main: int | None
) -> tuple[WebDriver | None, BaseException | None]:
    """Strategy 4: retry in headless mode.

    See :func:`_try_strategy_default` for the return contract.
    """
    driver: WebDriver | None = None
    try:
        headless_options = get_options(headless=True)
        if resolved_path:
            headless_options.binary_location = resolved_path
        driver = cast(
            WebDriver,
            _get_uc_module().Chrome(
                options=headless_options, version_main=version_main
            ),
        )
        LOGGER.debug("ChromeDriver started in headless mode.")
        return driver, None
    except Exception as err:  # noqa: BLE001
        _quit_driver(driver)
        LOGGER.warning("Strategy 4 (headless) failed: %s", err)
        return None, err


def _create_driver_inner(
    chrome_path: str | None = None,
    *,
    chrome_version: int | None = None,
    headless: bool = False,
) -> WebDriver:
    """Internal driver creation, orchestrating the strategy fallbacks.

    Resolution follows the layered chain CLI argument > environment variable >
    auto-detection for both the Chrome path and the Chrome version. The version
    is resolved independently of the path so an explicit version override still
    applies when no binary path can be located.
    """
    # Kill any existing Chrome processes to avoid conflicts
    _kill_existing_chrome_processes()

    # Resolve path and version from their three independent sources. The sources
    # are kept separate (no chrome_path-or-find_chrome collapse) so the env var
    # can take effect between an explicit argument and auto-detection.
    resolved_path, _path_source = _resolve_chrome_path(
        explicit=chrome_path,
        env_raw=os.environ.get(_ENV_CHROME_PATH),
        detected=find_chrome(),
    )
    detected_version = get_chrome_version(resolved_path) if resolved_path else None
    if detected_version:
        LOGGER.debug("Detected Chrome version: %d", detected_version)
    resolved_version, version_source = _resolve_chrome_version(
        explicit=chrome_version,
        env_raw=os.environ.get(_ENV_CHROME_VERSION),
        detected=detected_version,
    )
    if resolved_version is not None and resolved_path is None:
        LOGGER.warning(
            "Chrome version override is set (version=%s, source=%s) but no Chrome "
            "binary path could be resolved; undetected-chromedriver must locate "
            "Chrome on its own (binary_location/browser_executable_path unset).",
            resolved_version,
            version_source,
        )

    # Build the ordered list of strategy attempts. Strategy 2 only applies when
    # a path is resolved; strategy 3 (let uc pick the version) only when no
    # version could be resolved -- with a known version it would duplicate
    # strategy 1 and, in the old code, escalate to the latest stable driver;
    # strategy 4 (headless retry) only when not already headless. Every strategy
    # uses resolved_version so we never escalate to None while a version is
    # known. Each attempt returns (driver, saw_non_file_lock_error).
    attempts: list[Callable[[], tuple[WebDriver | None, BaseException | None]]] = [
        lambda: _try_strategy_default(
            resolved_path=resolved_path,
            version_main=resolved_version,
            headless=headless,
        )
    ]
    if resolved_path:
        attempts.append(
            lambda: _try_strategy_explicit_path(
                resolved_path=resolved_path,
                version_main=resolved_version,
                headless=headless,
            )
        )
    if resolved_version is None:
        attempts.append(
            lambda: _try_strategy_no_version(
                resolved_path=resolved_path, headless=headless
            )
        )
    if not headless:
        LOGGER.info("Trying headless mode...")
        attempts.append(
            lambda: _try_strategy_headless(
                resolved_path=resolved_path, version_main=resolved_version
            )
        )

    # Track whether all failures are file-lock related (Windows) and keep the
    # last error so we can surface a targeted version-mismatch hint.
    all_file_lock = True
    last_error: BaseException | None = None
    for attempt in attempts:
        driver, error = attempt()
        if driver is not None:
            return driver
        if error is not None:
            last_error = error
            if not _is_file_lock_error(error):
                all_file_lock = False

    # Strategy 5: webdriver-manager fallback
    fallback_driver = _try_webdriver_manager_fallback()
    if fallback_driver is not None:
        return fallback_driver

    # If all failures were file-lock related, raise PermissionError so the
    # outer retry loop in create_driver() can wait and retry.
    if all_file_lock and platform.system() == "Windows":
        raise PermissionError(
            "All ChromeDriver strategies failed due to file lock (WinError 32/183). "
            "A previous ChromeDriver process may still be running."
        )

    # All strategies failed -- report detected vs. used version and its source
    # so the failure is diagnosable instead of just echoing Selenium's raw line.
    _log_version_mismatch_hint(
        last_error,
        detected_version=detected_version,
        resolved_version=resolved_version,
        version_source=version_source,
    )
    raise RuntimeError(
        "Failed to start ChromeDriver after all attempts.\n"
        "Possible solutions:\n"
        "1. Make sure Google Chrome is installed and up-to-date\n"
        "2. Try: pip install --upgrade undetected-chromedriver selenium webdriver-manager\n"
        f"3. Current detected path: {resolved_path or 'None'}\n"
        f"4. detected: {detected_version or 'Unknown'}; "
        f"requested/used: {resolved_version or 'Unknown'}; source: {version_source}\n"
        "5. Check if Chrome is blocked by antivirus or firewall"
    )

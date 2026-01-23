from __future__ import annotations

import importlib
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from types import SimpleNamespace
from typing import Any, cast

from selenium.webdriver.chrome.webdriver import WebDriver

# Platform-specific import for Windows registry access
if platform.system() == "Windows":
    import winreg
else:
    winreg = None  # type: ignore[assignment]

# Optional imports for webdriver-manager fallback
try:
    from selenium import webdriver as selenium_webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager

    _WEBDRIVER_MANAGER_AVAILABLE = True
except ImportError:
    selenium_webdriver = None  # type: ignore[assignment]
    ChromeService = None  # type: ignore[assignment,misc]
    ChromeDriverManager = None  # type: ignore[assignment,misc]
    _WEBDRIVER_MANAGER_AVAILABLE = False

LOGGER = logging.getLogger(__name__)


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
            if winreg is not None:
                try:
                    key = winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"
                    )
                    version, _ = winreg.QueryValueEx(key, "version")
                    winreg.CloseKey(key)
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


def get_driver(chrome_path: str | None, *, headless: bool = False) -> WebDriver:
    """Initialize and return an undetected Chrome driver.

    Parameters
    ----------
    chrome_path: str
        Path to the Chrome executable.
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

    return cast(WebDriver, _get_uc_module().Chrome(options=options, version_main=None))


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

    try:
        LOGGER.info("Attempting webdriver-manager fallback...")
        service = ChromeService(ChromeDriverManager().install())
        options = selenium_webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = selenium_webdriver.Chrome(service=service, options=options)
        LOGGER.warning(
            "Started using webdriver-manager (standard Selenium). "
            "This uses standard Selenium without bot detection bypass!"
        )
        return driver
    except Exception as err:  # noqa: BLE001 - fallback should not raise
        LOGGER.debug("webdriver-manager fallback failed: %s", err)
        return None


def safe_quit_driver(driver: WebDriver | None) -> None:
    """Safely quit the Chrome driver, handling WinError 6 and other errors.

    Parameters
    ----------
    driver: WebDriver | None
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


def create_driver(
    chrome_path: str | None = None, *, headless: bool = False
) -> WebDriver:
    """Backward-compatible wrapper for driver creation with multiple fallbacks.

    Attempts driver creation in this order:
    1. Standard creation with version_main for Chrome version compatibility
    2. Fallback with explicit Chrome path from system
    3. Fallback without specifying version
    4. Headless mode
    5. webdriver-manager fallback (standard Selenium)
    """
    # Kill any existing Chrome processes to avoid conflicts
    _kill_existing_chrome_processes()

    resolved_path = chrome_path or find_chrome()
    version_main: int | None = None

    if resolved_path:
        version_main = get_chrome_version(resolved_path)
        if version_main:
            LOGGER.debug("Detected Chrome version: %d", version_main)

    # Strategy 1: Default with version_main if detected
    try:
        options = get_options(headless=headless)
        if resolved_path:
            options.binary_location = resolved_path
        driver = cast(
            WebDriver,
            _get_uc_module().Chrome(options=options, version_main=version_main),
        )
        LOGGER.debug("ChromeDriver started successfully.")
        return driver
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("Strategy 1 (default) failed: %s", err)

    # Strategy 2: Use browser_executable_path parameter (if supported)
    if resolved_path:
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
            return driver
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Strategy 2 (explicit path) failed: %s", err)

    # Strategy 3: Try without specifying version
    try:
        options = get_options(headless=headless)
        if resolved_path:
            options.binary_location = resolved_path
        driver = cast(
            WebDriver, _get_uc_module().Chrome(options=options, version_main=None)
        )
        LOGGER.debug("ChromeDriver started without explicit version.")
        return driver
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("Strategy 3 (no version) failed: %s", err)

    # Strategy 4: Try headless mode
    if not headless:
        LOGGER.info("Trying headless mode...")
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
            return driver
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("Strategy 4 (headless) failed: %s", err)

    # Strategy 5: webdriver-manager fallback
    driver = _try_webdriver_manager_fallback()
    if driver:
        return driver

    # All strategies failed
    raise RuntimeError(
        "Failed to start ChromeDriver after all attempts.\n"
        "Possible solutions:\n"
        "1. Make sure Google Chrome is installed and up-to-date\n"
        "2. Try: pip install --upgrade undetected-chromedriver selenium webdriver-manager\n"
        f"3. Current detected path: {resolved_path or 'None'}\n"
        f"4. Current detected version: {version_main or 'Unknown'}\n"
        "5. Check if Chrome is blocked by antivirus or firewall"
    )

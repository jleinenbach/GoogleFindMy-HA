from __future__ import annotations

import importlib
import logging
import os
import platform
import shutil
from types import SimpleNamespace
from typing import Any, cast

from selenium.webdriver.chrome.webdriver import WebDriver

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
uc: Any = SimpleNamespace(ChromeOptions=None, Chrome=None)


def get_uc() -> Any:
    """Lazily import ``undetected_chromedriver``."""

    if _UC_CACHE.module is None:
        if getattr(uc, "Chrome", None) is not None and getattr(uc, "ChromeOptions", None) is not None:
            _UC_CACHE.module = uc
        else:
            _UC_CACHE.module = cast(Any, _load_uc())

        globals()["uc"] = _UC_CACHE.module

    return _UC_CACHE.module
type ChromeOptions = Any


def find_chrome() -> str | None:
    """Locate the Chrome executable on the current system.

    Returns
    -------
    str | None
        The absolute path to the Chrome binary if it could be resolved, otherwise ``None``.
    """

    possible_paths = [
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\ProgramData\\chocolatey\\bin\\chrome.exe",
        r"C:\\Users\\%USERNAME%\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        "/opt/google/chrome/chrome",
        "/snap/bin/chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]

    # Check predefined paths
    for path in possible_paths:
        if os.path.exists(path):
            return path

    # Use system command to find Chrome
    try:
        if platform.system() == "Windows":
            chrome_path = shutil.which("chrome")
        else:
            chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
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
    uc.ChromeOptions
        The configured Chrome options instance.
    """

    chrome_options = get_uc().ChromeOptions()
    if not headless:
        chrome_options.add_argument("--start-maximized")
    else:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--no-sandbox")

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

    return cast(WebDriver, get_uc().Chrome(options=options))


def create_driver(chrome_path: str | None = None, *, headless: bool = False) -> WebDriver:
    """Backward-compatible wrapper for driver creation."""
    try:
        return get_driver(chrome_path, headless=headless)
    except Exception as err:  # noqa: BLE001
        LOGGER.warning("Default ChromeDriver startup failed: %s", err)

        fallback_path = chrome_path or find_chrome()
        if fallback_path is None:
            raise FileNotFoundError(
                "Chrome binary not found; install Chrome or provide chrome_path"
            ) from err

        fallback_options = get_options(headless=headless)
        fallback_options.binary_location = fallback_path
        try:
            return cast(WebDriver, get_uc().Chrome(options=fallback_options))
        except Exception as fallback_err:  # noqa: BLE001
            LOGGER.warning(
                "ChromeDriver failed using system binary: %s", fallback_err
            )
            raise RuntimeError(
                "Chrome driver startup failed using bundled and system binaries"
            ) from fallback_err

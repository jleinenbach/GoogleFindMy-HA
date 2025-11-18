import importlib
import logging
import os
import platform
import shutil
from typing import Any, TypeAlias, cast

from selenium.webdriver.chrome.webdriver import WebDriver

uc = cast(Any, importlib.import_module("undetected_chromedriver"))
ChromeOptions: TypeAlias = Any

LOGGER = logging.getLogger(__name__)


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

    chrome_options = uc.ChromeOptions()
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

    return cast(WebDriver, uc.Chrome(options=options))


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
            return cast(WebDriver, uc.Chrome(options=fallback_options))
        except Exception as fallback_err:  # noqa: BLE001
            LOGGER.warning(
                "ChromeDriver failed using system binary: %s", fallback_err
            )
            raise RuntimeError(
                "Chrome driver startup failed using bundled and system binaries"
            ) from fallback_err

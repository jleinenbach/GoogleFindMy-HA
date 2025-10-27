# custom_components/googlefindmy/chrome_driver.py
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import logging
import os
import platform
import shutil
from typing import cast

import undetected_chromedriver as uc
from selenium.webdriver.chrome.webdriver import WebDriver


LOGGER = logging.getLogger(__name__)


def find_chrome() -> str | None:
    """Locate the Chrome executable on the current system.

    Returns
    -------
    str | None
        The absolute path to the Chrome binary if it could be resolved, otherwise ``None``.
    """

    possible_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\ProgramData\chocolatey\bin\chrome.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Google\Chrome\Application\chrome.exe",
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


def get_options(*, headless: bool = False) -> uc.ChromeOptions:
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


def create_driver(*, headless: bool = False) -> WebDriver:
    """Create an undetected Chrome WebDriver configured for authentication flows.

    Parameters
    ----------
    headless: bool
        Whether the browser session should avoid rendering a visible window.

    Returns
    -------
    WebDriver
        An active Selenium WebDriver instance.

    Raises
    ------
    RuntimeError
        If a compatible Chrome binary cannot be located or the driver fails to start.
    """

    try:
        chrome_options = get_options(headless=headless)
        driver = cast(WebDriver, uc.Chrome(options=chrome_options))
        LOGGER.info("ChromeDriver started with bundled binary")
        return driver
    except Exception:  # pragma: no cover - relies on external binary availability
        LOGGER.warning(
            "Default ChromeDriver startup failed; attempting to locate a system Chrome binary",
        )

        chrome_path = find_chrome()
        if chrome_path:
            chrome_options = get_options(headless=headless)
            chrome_options.binary_location = chrome_path
            try:
                driver = cast(WebDriver, uc.Chrome(options=chrome_options))
                LOGGER.info(
                    "ChromeDriver started using system binary at %s", chrome_path
                )
                return driver
            except (
                Exception
            ):  # pragma: no cover - depends on external binary availability
                LOGGER.exception(
                    "ChromeDriver failed using system binary at %s",
                    chrome_path,
                )
        else:
            LOGGER.error("Chrome executable not found in known paths")

        raise RuntimeError(
            "Failed to start ChromeDriver. A compatible Chrome installation was not detected. "
            "Update Chrome to the latest version or configure the binary path manually."
        )


if __name__ == "__main__":
    create_driver()

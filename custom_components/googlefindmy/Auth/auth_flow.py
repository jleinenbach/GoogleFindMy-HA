# custom_components/googlefindmy/Auth/auth_flow.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, cast

from selenium.webdriver.support.ui import WebDriverWait

from custom_components.googlefindmy.chrome_driver import create_driver, safe_quit_driver

if TYPE_CHECKING:  # pragma: no cover - import-time typing block
    from selenium.webdriver.remote.webdriver import WebDriver


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the standalone auth flow helper."""

    parser = argparse.ArgumentParser(
        description="Run the Google account OAuth login flow via Chrome."
    )
    parser.add_argument(
        "--chrome-path",
        default=None,
        help="Path to the Chrome/Chromium binary (overrides auto-detection).",
    )
    parser.add_argument(
        "--chrome-version",
        type=int,
        default=None,
        help=(
            "Chrome major version to pin (e.g. 149). Overrides auto-detection; "
            "useful when the stable channel is ahead of your installed Chrome."
        ),
    )
    return parser.parse_args(argv)


def request_oauth_account_token_flow(
    headless: bool = False,
    *,
    chrome_path: str | None = None,
    chrome_version: int | None = None,
) -> tuple[str, str | None]:
    """Open Chrome for Google login and return ``(oauth_token, email)``.

    The *email* is extracted from the Chrome session after login if possible.
    It may be ``None`` when the DOM selectors fail (e.g. Google changes their
    page layout) — callers should fall back to prompting the user.
    """
    # In Home Assistant context, skip the interactive prompts
    is_home_assistant = "homeassistant" in sys.modules

    if not headless and not is_home_assistant:
        print("""[AuthFlow] This script will now open Google Chrome on your device to login to your Google account.
> Please make sure that Chrome is installed on your system.
> For macOS users only: Make that you allow Python (or PyCharm) to control Chrome if prompted.
        """)

        # Press enter to continue
        input("[AuthFlow] Press Enter to continue...")

    # Automatically install and set up the Chrome driver
    if not is_home_assistant:
        print("[AuthFlow] Installing ChromeDriver...")

    driver: WebDriver = create_driver(
        chrome_path=chrome_path, chrome_version=chrome_version, headless=headless
    )

    try:
        # Open the browser and navigate to the URL
        driver.get("https://accounts.google.com/EmbeddedSetup")

        # Wait until the "oauth_token" cookie is set
        if not is_home_assistant:
            print("[AuthFlow] Waiting for 'oauth_token' cookie to be set...")
        WebDriverWait(driver, 300).until(
            lambda d: d.get_cookie("oauth_token") is not None
        )

        # Get the value of the "oauth_token" cookie
        cookie = driver.get_cookie("oauth_token")
        if cookie is None:
            msg = "OAuth token cookie missing despite wait completion"
            raise RuntimeError(msg)

        oauth_token_cookie: dict[str, Any] = cast(dict[str, Any], cookie)
        oauth_token_value = oauth_token_cookie.get("value")
        if not isinstance(oauth_token_value, str):
            msg = "OAuth token cookie value is missing or not a string"
            raise RuntimeError(msg)

        # Try to extract the logged-in email from the page before closing.
        email: str | None = _extract_email_from_session(driver)

        # Print the value of the "oauth_token" cookie
        if not is_home_assistant:
            print("[AuthFlow] Retrieved Account Token successfully.")
            if email:
                print(f"[AuthFlow] Detected account: {email}")

        return oauth_token_value, email

    finally:
        # Close the browser (safe_quit handles WinError 6 on Windows)
        safe_quit_driver(driver)


def _extract_email_from_session(driver: WebDriver) -> str | None:
    """Best-effort extraction of the Google account email from the Chrome session.

    Google's EmbeddedSetup page stores the email in various places after login.
    We try several strategies; if none works we return ``None`` and the caller
    can fall back to a manual prompt.
    """
    # Strategy 1: data-email attribute (common in Google account pages)
    for selector in (
        "[data-email]",
        "#profileIdentifier",
    ):
        try:
            result = driver.execute_script(
                f"var el = document.querySelector('{selector}');"
                "if (!el) return null;"
                "return el.dataset.email || el.textContent || null;"
            )
            if isinstance(result, str) and "@" in result:
                return result.strip()
        except Exception:  # noqa: BLE001
            continue

    # Strategy 2: scan all cookies for one that looks like an email
    try:
        for c in driver.get_cookies():
            val = c.get("value", "")
            if isinstance(val, str) and "@" in val and "." in val.split("@")[-1]:
                return val.strip()
    except Exception:  # noqa: BLE001
        pass

    return None


if __name__ == "__main__":
    _args = _parse_cli_args()
    request_oauth_account_token_flow(
        chrome_path=_args.chrome_path, chrome_version=_args.chrome_version
    )

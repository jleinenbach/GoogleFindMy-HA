#!/usr/bin/env python3
# custom_components/googlefindmy/get_oauth_token.py
"""Standalone helper to obtain an OAuth token for the Google Find My integration."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver


# Add the current directory to path so imports work
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

# Also add parent directories to handle custom_components structure
parent_dir = os.path.dirname(current_dir)
if "custom_components" in current_dir:
    # If we're inside custom_components, add the parent of custom_components
    sys.path.insert(0, os.path.dirname(parent_dir))


COOKIE_NAME = "oauth_token"


def _cookie_value(cookie: Mapping[str, Any] | None) -> str | None:
    """Return the token string from a Selenium cookie mapping."""

    if cookie is None:
        return None

    value = cookie.get("value")
    return cast(str | None, value)


def main() -> None:
    """Get OAuth token for Google Find My Device."""

    print("=" * 60)
    print("Google Find My Device - OAuth Token Generator")
    print("=" * 60)
    print()

    try:
        from selenium.webdriver.support.ui import WebDriverWait

        from custom_components.googlefindmy.chrome_driver import create_driver
    except ImportError as err:
        print(f"Missing required package: {err}")
        print()
        print("Please install the required packages:")
        print("pip install selenium undetected-chromedriver")
        sys.exit(1)

    try:
        driver: WebDriver = create_driver(headless=False)
    except Exception as err:
        print(f"Error: {err}")
        print()
        print("Make sure you have Chrome installed and try again.")
        sys.exit(1)

    print("This script will open Chrome to authenticate with Google.")
    print("After logging in, the OAuth token will be displayed.")
    input("Press Enter to continue...\n")

    print("Opening Chrome browser...")

    try:
        # Open the browser and navigate to the URL
        driver.get("https://accounts.google.com/EmbeddedSetup")

        # Wait until the "oauth_token" cookie is set
        print(
            "Waiting for authentication... Please complete the login process in the browser."
        )
        WebDriverWait(driver, 300).until(
            lambda browser: browser.get_cookie(COOKIE_NAME) is not None
        )

        # Get the value of the "oauth_token" cookie
        oauth_token_value = _cookie_value(driver.get_cookie(COOKIE_NAME))

        if oauth_token_value:
            print()
            print("=" * 60)
            print("SUCCESS! Your OAuth token is:")
            print("=" * 60)
            print(oauth_token_value)
            print("=" * 60)
            print()
            print("Copy this token and paste it in Home Assistant when")
            print("configuring the Google Find My Device integration.")
            print("Choose 'Manual Token Entry' as the authentication method.")
            print()
            input("Press Enter to exit...\n")
        else:
            print("Failed to obtain OAuth token.")
            sys.exit(1)
    finally:
        # Close the browser
        driver.quit()


if __name__ == "__main__":
    main()

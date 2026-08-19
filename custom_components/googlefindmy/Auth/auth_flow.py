# custom_components/googlefindmy/Auth/auth_flow.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING, Any, cast

from custom_components.googlefindmy.browser_deps import missing_browser_dependency

try:
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError as _err:  # pragma: no cover - needs a selenium-less environment
    raise missing_browser_dependency(_err) from _err

from custom_components.googlefindmy.chrome_driver import create_driver, safe_quit_driver

if TYPE_CHECKING:  # pragma: no cover - import-time typing block
    from selenium.webdriver.remote.webdriver import WebDriver

# Opt-in for consoles that carry a user but report no tty (IDE run windows).
_ENV_ASSUME_INTERACTIVE = "GOOGLEFINDMY_ASSUME_INTERACTIVE"


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


def _stdin_is_attended() -> bool:
    """Return True when a human can answer a prompt on standard input.

    ``isatty`` is the signal, with one escape hatch: an IDE console (PyCharm,
    VS Code) proxies stdin through a pipe and reports ``isatty() == False``
    while a user is very much sitting in front of it, and the desktop prompt
    right below explicitly addresses PyCharm users. Setting
    ``GOOGLEFINDMY_ASSUME_INTERACTIVE=1`` asserts "there is someone here" for
    exactly that case; it is a deliberate opt-in, so an unattended process does
    not get the same treatment by accident.

    ``sys.stdin`` can be ``None`` (pythonw, some embeddings) and ``isatty`` can
    raise on a closed stream, so both are treated as "nobody there".
    """
    if os.environ.get(_ENV_ASSUME_INTERACTIVE) == "1":
        return True
    stream = sys.stdin
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


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
    # ``headless`` is the interactivity signal: an automated caller passes True,
    # an attended terminal session passes False. The historical test
    # ``"homeassistant" in sys.modules`` is NOT usable for this and was removed:
    # it is true in *every* standalone CLI run, either because Home Assistant is
    # installed and pulled in by the import graph, or because main.py injects
    # ``homeassistant.*`` stubs into sys.modules itself when it is not. The CLI
    # therefore fabricated exactly the signal the heuristic read, which silently
    # disabled the desktop gate below and let an unattended process open a
    # browser (measured 2026-07-28). Note that the integration itself never calls
    # this function: the only production caller is main.py (the standalone CLI).
    #
    # Inside the docker-login container Chrome runs *in the container* and is
    # driven through the noVNC viewer, not on the user's own desktop. The
    # entrypoint sets GOOGLEFINDMY_CONTAINER_LOGIN=1 there, so without this signal
    # the user would be told to "install Chrome on your system" -- wrong for the
    # container, where they must open the noVNC URL.
    is_container = os.environ.get("GOOGLEFINDMY_CONTAINER_LOGIN") == "1"

    if not headless:
        if is_container:
            novnc_url = (
                os.environ.get("GOOGLEFINDMY_NOVNC_URL") or "http://localhost:7900"
            )
            # The entrypoint mints a per-run password and exports it here for a
            # LAN-hardened bind; on the loopback default it stays unset and the
            # base image's fixed "secret" is what the viewer actually accepts.
            novnc_password = os.environ.get("GOOGLEFINDMY_NOVNC_PASSWORD", "secret")
            print(
                "[AuthFlow] ==================================================\n"
                "[AuthFlow] Action required to sign in to Google:\n"
                f"[AuthFlow]   1. Open {novnc_url} in your browser "
                f"(password: {novnc_password}).\n"
                "[AuthFlow]   2. Chrome opens by itself in that view within a "
                "few seconds.\n"
                "[AuthFlow]   3. Sign in to Google there, then come back to "
                "this terminal.\n"
                "[AuthFlow] =================================================="
            )
            # No stdin gate in the container: Chrome runs *inside* the container
            # and the user drives it through noVNC. Blocking on input() here
            # would leave that viewer showing an empty desktop until someone
            # pressed Enter in a terminal they may not even be looking at, and
            # the pre-start prompt used to arrive before the display was ready.
            # The terminal still has to stay attached, because main.py asks for
            # the account e-mail on stdin when auto-detection fails.
        else:
            print("""[AuthFlow] This script will now open Google Chrome on your device to login to your Google account.
> Please make sure that Chrome is installed on your system.
> For macOS users only: Make that you allow Python (or PyCharm) to control Chrome if prompted.
        """)

            # Press enter to continue: on the desktop path Chrome takes over the
            # user's own screen, so they get to decide when that happens. No
            # terminal means nobody is there to decide, and launching a browser
            # (plus its process cleanup) unattended is what let a test subprocess
            # reach the Chrome flow at all. Abort before create_driver().
            #
            # The check is ``isatty``, not "does the read fail": a pipe supplies
            # bytes without a human, and consuming a line here would eat the
            # account e-mail that main.py reads from the same stdin afterwards.
            # A non-tty is therefore refused rather than read. The desktop login
            # cannot be scripted anyway -- signing in to Google happens by hand in
            # the browser window -- so nothing usable is lost.
            if not _stdin_is_attended():
                msg = (
                    "[AuthFlow] The interactive Chrome login needs an attended "
                    "terminal (stdin is not a terminal). Run it from a terminal; "
                    f"set {_ENV_ASSUME_INTERACTIVE}=1 if you are sitting at an "
                    "IDE console that proxies stdin; use the docker-login "
                    "container (GOOGLEFINDMY_CONTAINER_LOGIN=1); or call the "
                    "flow with headless=True."
                )
                raise RuntimeError(msg)
            try:
                input("[AuthFlow] Press Enter to continue...")
            except EOFError as err:
                # The terminal was closed between the check and the read.
                msg = (
                    "[AuthFlow] Standard input closed while waiting for "
                    "confirmation; not starting Chrome."
                )
                raise RuntimeError(msg) from err

    # Automatically install and set up the Chrome driver
    if not headless:
        print("[AuthFlow] Installing ChromeDriver...")

    driver: WebDriver = create_driver(
        chrome_path=chrome_path, chrome_version=chrome_version, headless=headless
    )

    try:
        # Open the browser and navigate to the URL
        driver.get("https://accounts.google.com/EmbeddedSetup")

        # Wait until the "oauth_token" cookie is set
        if not headless:
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
        if not headless:
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

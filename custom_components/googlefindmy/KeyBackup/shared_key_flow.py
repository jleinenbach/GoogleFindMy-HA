# custom_components/googlefindmy/KeyBackup/shared_key_flow.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from custom_components.googlefindmy.browser_deps import (
    BrowserPackagesUnusable,
    missing_browser_dependency,
)

try:
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError as _err:  # pragma: no cover - needs a selenium-less environment
    raise missing_browser_dependency(_err) from _err

from custom_components.googlefindmy.chrome_driver import create_driver, safe_quit_driver
from custom_components.googlefindmy.KeyBackup.response_parser import (
    get_fmdn_shared_key,
)
from custom_components.googlefindmy.KeyBackup.shared_key_request import (
    get_security_domain_request_url,
)
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    FETCH_SCRIPT,
    FIND_MY_DEVICE_URL,
    SharedKeyFlowDeadlineError,
    VaultCause,
    VaultFetchResult,
    parse_batchexecute,
)
from custom_components.googlefindmy.redaction import describe_keys, describe_payload

if TYPE_CHECKING:  # pragma: no cover - import-time typing block
    from selenium.webdriver.remote.webdriver import WebDriver


LOGGER = logging.getLogger(__name__)

# The interactive sign-in already waits this long (`WebDriverWait(driver, 300)`);
# the alert loop gets the same budget instead of waiting forever for a page that
# never answers. A constant on purpose, not a user setting.
ALERT_DEADLINE_S: Final = 300
SIGN_IN_TIMEOUT_S: Final = 300
# Upper bound for loading the Find My Device page and for the in-page fetch.
VAULT_FETCH_TIMEOUT_S: Final = 30
# Seam for the clock, so tests can drive the deadline without sleeping.
_monotonic = time.monotonic

# The page is ready for the fetch once its batchexecute parameters exist;
# `SNlM0e` is the anti-CSRF value the fetch script sends as `at`.
_WIZ_READY_SCRIPT = (
    "return Boolean(window.WIZ_global_data && window.WIZ_global_data.SNlM0e);"
)

# Alert payloads on this channel carry vault key material. Anything that reaches
# a log record is reduced to shape: type, length, key names. Redacting by key
# name would not be enough here, because these branches exist precisely for
# payloads whose shape is unknown, where the sensitive field may sit under a name
# nobody anticipated.


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the standalone shared key helper."""

    parser = argparse.ArgumentParser(
        description="Run the manual shared key retrieval flow via Chrome."
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


def _result_from_answer(answer: object) -> VaultFetchResult:
    """Turn the callback value of `FETCH_SCRIPT` into a `VaultFetchResult`.

    The script hands back `{ok, status, text}` or `{ok: false, error}`. Anything
    but a successful answer with a text body is `http_status`; neither the status
    code nor the error text is kept, because only the cause may be reported.
    """

    if not isinstance(answer, dict) or answer.get("ok") is not True:
        return VaultFetchResult((), VaultCause.HTTP_STATUS)
    text = answer.get("text")
    if not isinstance(text, str):
        return VaultFetchResult((), VaultCause.HTTP_STATUS)
    return parse_batchexecute(text)


def fetch_vault_entries(driver: WebDriver) -> VaultFetchResult:
    """Fetch every vault entry of the signed-in account; never raises.

    Navigates the already signed-in session to `FIND_MY_DEVICE_URL`, waits until
    the page exposes its batchexecute parameters and runs `FETCH_SCRIPT` there,
    because the request needs the cookies and `WIZ_global_data` of that session.
    A navigation, wait or script failure becomes `http_status`; the parser
    assigns every other cause. Only the entry count and the cause are logged.
    """

    try:
        driver.get(FIND_MY_DEVICE_URL)
        driver.set_script_timeout(VAULT_FETCH_TIMEOUT_S)
        WebDriverWait(driver, VAULT_FETCH_TIMEOUT_S).until(
            lambda page: page.execute_script(_WIZ_READY_SCRIPT)
        )
        answer = driver.execute_async_script(FETCH_SCRIPT)
    except Exception:  # every driver failure maps to one cause
        result = VaultFetchResult((), VaultCause.HTTP_STATUS)
    else:
        result = _result_from_answer(answer)
    LOGGER.debug("Vault fetch: %s entries, cause=%s", len(result.entries), result.cause)
    return result


def request_shared_key_flow(  # noqa: PLR0915 - the alert loop is one protocol
    *,
    chrome_path: str | None = None,
    chrome_version: int | None = None,
    vault_sink: Callable[[VaultFetchResult], None] | None = None,
) -> str | None:
    """Execute the manual shared key retrieval flow via Selenium.

    With `vault_sink`, the vault entries are fetched once in the same session,
    after sign-in and before the unlock page, and handed to the sink. Neither a
    failed fetch nor a sink that raises changes the outcome of the flow.
    """

    driver: WebDriver | None = None
    try:
        driver = create_driver(chrome_path=chrome_path, chrome_version=chrome_version)
    except BrowserPackagesUnusable:
        # Not a runtime Selenium failure, and the only one worth telling apart
        # here: returning `None` for it erases the single piece of information
        # the user needs, and every caller then has to guess it back from a
        # message that no longer says it.
        raise
    except Exception:  # pragma: no cover - relies on runtime Selenium setup
        LOGGER.exception("Failed to initialize ChromeDriver for shared key flow")
        return None

    try:
        driver.get("https://accounts.google.com/")

        WebDriverWait(driver, 300).until(
            ec.url_contains("https://myaccount.google.com")
        )
        LOGGER.info("Signed in successfully during shared key flow")

        if vault_sink is not None:
            # Own `try`: an exception from the sink must not reach the outer
            # handler below, which would end the flow with `None` before the
            # unlock page ever opened.
            try:
                vault_sink(fetch_vault_entries(driver))
            except Exception as exc:
                LOGGER.debug("Vault sink raised: %s", type(exc).__name__)

        security_url = get_security_domain_request_url()
        driver.get(security_url)

        script = """
        window.mm = {
            setVaultSharedKeys: function(str, vaultKeys) {
                console.log('setVaultSharedKeys called with:', str, vaultKeys);
                alert(JSON.stringify({ method: 'setVaultSharedKeys', str: str, vaultKeys: vaultKeys }));
            },
            closeView: function() {
                console.log('closeView called');
                alert(JSON.stringify({ method: 'closeView' }));
            }
        };
        """
        driver.execute_script(script)

        deadline = _monotonic() + ALERT_DEADLINE_S
        while _monotonic() < deadline:
            try:
                WebDriverWait(driver, 0.5).until(ec.alert_is_present())
            except TimeoutException:
                continue

            alert = driver.switch_to.alert
            message: str = alert.text
            alert.accept()

            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                LOGGER.warning(
                    "Discarding malformed alert payload (%s)",
                    describe_payload(message),
                )
                continue

            method = data.get("method")
            if method == "setVaultSharedKeys":
                vault_keys = data.get("vaultKeys")
                if not isinstance(vault_keys, str):
                    LOGGER.error(
                        "Missing or invalid vaultKeys payload (%s); alert %s",
                        describe_payload(vault_keys),
                        describe_keys(data),
                    )
                    continue

                shared_key = get_fmdn_shared_key(vault_keys)
                shared_key_hex: str = shared_key.hex()
                LOGGER.info("Received shared key from authentication flow")
                return shared_key_hex

            if method == "closeView":
                LOGGER.info("closeView invoked; terminating browser session")
                return None

            LOGGER.debug("Unhandled alert payload: %s", describe_keys(data))

        LOGGER.warning("Shared key flow ended: cause=%s", VaultCause.ALERT_DEADLINE)

    except Exception:  # pragma: no cover - runtime Selenium failures
        LOGGER.exception("Shared key flow terminated unexpectedly")
        return None
    finally:
        safe_quit_driver(driver)
    # Only the deadline reaches this line; raised after the driver quit.
    raise SharedKeyFlowDeadlineError(
        f"The sign-in was not finished within {ALERT_DEADLINE_S} s."
    )


def request_vault_entries_flow(
    *, chrome_path: str | None = None, chrome_version: int | None = None
) -> VaultFetchResult:
    """Sign in and fetch the vault entries, without opening the unlock page.

    For a cached shared key whose bundle lacks the version field: the user only
    goes through the Google sign-in, the shared key is neither requested nor
    changed. `BrowserPackagesUnusable` passes through like in
    `request_shared_key_flow`; every other failure, including a sign-in that
    does not finish in time, becomes `http_status`. Never raises otherwise.
    """

    driver: WebDriver | None = None
    try:
        driver = create_driver(chrome_path=chrome_path, chrome_version=chrome_version)
    except BrowserPackagesUnusable:
        raise
    except Exception as exc:
        # Type only, like the handler below: a driver error message can carry
        # page content.
        LOGGER.warning(
            "Failed to initialize ChromeDriver for vault entries flow (%s)",
            type(exc).__name__,
        )
        return VaultFetchResult((), VaultCause.HTTP_STATUS)

    try:
        driver.get("https://accounts.google.com/")
        WebDriverWait(driver, SIGN_IN_TIMEOUT_S).until(
            ec.url_contains("https://myaccount.google.com")
        )
        LOGGER.info("Signed in successfully during vault entries flow")
        return fetch_vault_entries(driver)
    except Exception as exc:
        # `TimeoutException` when the sign-in does not finish; only the type is
        # logged, the message of a driver error can carry page content.
        LOGGER.warning(
            "Vault entries flow ended: cause=%s (%s)",
            VaultCause.HTTP_STATUS,
            type(exc).__name__,
        )
        return VaultFetchResult((), VaultCause.HTTP_STATUS)
    finally:
        safe_quit_driver(driver)


def _main() -> int:
    """Run the flow as a command and report the outcome in the exit status.

    `request_shared_key_flow` returns `None` for most failures, including the
    one that got more likely when the browser packages left `manifest.json`:
    they are simply not installed. A caller that ignores that return exits 0
    on a run that retrieved nothing, and a script around it cannot tell the
    difference. The packages are named here because at this point they are the
    likeliest reason, and this is the last place that still knows. An unlock
    page that is not completed within ``ALERT_DEADLINE_S`` raises
    `SharedKeyFlowDeadlineError`; it is reported as a timeout, not a traceback.
    """

    args = _parse_cli_args()
    try:
        key = request_shared_key_flow(
            chrome_path=args.chrome_path, chrome_version=args.chrome_version
        )
    except BrowserPackagesUnusable as err:
        print(f"\n{err}\n", file=sys.stderr)
        return 1
    except SharedKeyFlowDeadlineError:
        print(
            "\nThe unlock page was not completed within "
            f"{ALERT_DEADLINE_S // 60} minutes. Run the tool again.",
            file=sys.stderr,
        )
        return 1
    if key:
        print(key)
        return 0

    from custom_components.googlefindmy.browser_deps import (  # noqa: PLC0415
        MISSING_BROWSER_PACKAGES_HINT,
        browser_packages_missing,
    )

    print(
        "\nThe shared key could not be retrieved. See the log above.", file=sys.stderr
    )
    if browser_packages_missing():
        print(f"\n{MISSING_BROWSER_PACKAGES_HINT}\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())

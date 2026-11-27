# custom_components/googlefindmy/KeyBackup/shared_key_flow.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import argparse
import functools
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
    from selenium.common.exceptions import (
        NoSuchWindowException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError as _err:  # pragma: no cover - needs a selenium-less environment
    raise missing_browser_dependency(_err) from _err

from custom_components.googlefindmy.Auth.auth_flow import (
    LoginWindowClosed,
    describe_lost_session,
    restart_on_closed_window,
)
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
    SharedKeyCancelledOnPage,
    SharedKeyFlowDeadlineError,
    SharedKeyStepAborted,
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

# What the user reads when sign-in 2 of 2 ends because of them. Sign-in 1 of 2
# has already written its tokens (`_FileCache.set` saves at once), so a new run
# only repeats this step; saying so keeps the user from starting over in vain.
WINDOW_CLOSED_MESSAGE: Final = (
    "[AuthFlow] The sign-in window 2 of 2 was closed before Google sent the key. "
    "The result of sign-in 1 of 2 is kept; run the login again to finish "
    "sign-in 2 of 2."
)
CANCELLED_ON_PAGE_MESSAGE: Final = (
    "[AuthFlow] Sign-in 2 of 2 was cancelled on the Google page before it sent "
    "the key. The result of sign-in 1 of 2 is kept; run the login again to "
    "finish sign-in 2 of 2."
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
    A navigation, wait or script failure becomes `http_status`, unless it says
    the window was closed: that is `window_closed`, so the caller can open the
    step again instead of reporting a failed request. The parser assigns every
    other cause. Only the entry count and the cause are logged.
    """

    try:
        driver.get(FIND_MY_DEVICE_URL)
        driver.set_script_timeout(VAULT_FETCH_TIMEOUT_S)
        WebDriverWait(driver, VAULT_FETCH_TIMEOUT_S).until(
            lambda page: page.execute_script(_WIZ_READY_SCRIPT)
        )
        answer = driver.execute_async_script(FETCH_SCRIPT)
    except Exception as exc:  # every driver failure maps to one cause
        result = VaultFetchResult((), _failure_cause(exc))
    else:
        result = _result_from_answer(answer)
    LOGGER.debug("Vault fetch: %s entries, cause=%s", len(result.entries), result.cause)
    return result


SIGNED_IN_URL_FRAGMENT: Final = "https://myaccount.google.com"


def _signed_in_or_window_gone(driver: WebDriver) -> bool:
    """Wait predicate of the sign-in: True once Google shows the account page.

    A closed window does not always make the next command raise. Through the
    Selenium grid on the QNAP host, ``current_url`` answered ``None`` after the
    user closed the window of sign-in 2, and ``ec.url_contains`` then failed
    with ``TypeError``, which ended the login instead of reopening the window.
    ``None`` is therefore checked against the open windows: none left raises
    ``NoSuchWindowException``, which ``until`` passes through unchanged and
    ``describe_lost_session`` classifies as a closed window; an open window
    keeps the wait going. If asking for the windows raises, that error is
    classified the same way as any other command's.
    """
    url = driver.current_url
    if url is None:
        if not driver.window_handles:
            raise NoSuchWindowException("no such window: the sign-in window was closed")
        return False
    return SIGNED_IN_URL_FRAGMENT in url


def _failure_cause(exc: BaseException) -> VaultCause:
    """Return `window_closed` for a window the user closed, else `http_status`."""

    if isinstance(exc, WebDriverException) and describe_lost_session(exc):
        return VaultCause.WINDOW_CLOSED
    return VaultCause.HTTP_STATUS


def request_shared_key_flow(
    *,
    chrome_path: str | None = None,
    chrome_version: int | None = None,
    vault_sink: Callable[[VaultFetchResult], None] | None = None,
) -> str | None:
    """Run sign-in 2 of 2; reopen its window at most twice if the user closes it.

    The restart lives here, on the one dynamic route Home Assistant code has to
    the browser (`shared_key_retrieval` -> this module), behind the terminal
    guard there; the once-per-process guard counts retrievals, not windows. A
    window closed once more than it is reopened raises `SharedKeyStepAborted`
    with the message for the terminal. `SharedKeyCancelledOnPage` and the
    deadline are never restarted.
    """
    try:
        return restart_on_closed_window(
            functools.partial(
                _shared_key_flow_once,
                chrome_path=chrome_path,
                chrome_version=chrome_version,
                vault_sink=vault_sink,
            ),
            step=2,
            total=2,
        )
    except LoginWindowClosed as err:
        raise SharedKeyStepAborted(str(err)) from err


def _shared_key_flow_once(  # noqa: PLR0912, PLR0915 - the alert loop is one protocol
    *,
    chrome_path: str | None,
    chrome_version: int | None,
    vault_sink: Callable[[VaultFetchResult], None] | None,
) -> str | None:
    """Execute the manual shared key retrieval flow via Selenium, one window.

    With `vault_sink`, the vault entries are fetched once in the same session,
    after sign-in and before the unlock page, and handed to the sink. Neither a
    failed fetch nor a sink that raises changes the outcome of the flow.

    Raises `LoginWindowClosed` when the user closes the window, the vault fetch
    included; the sink then receives nothing, because the step is opened again
    and the sink keeps only the first result it gets. Raises
    `SharedKeyCancelledOnPage` when the unlock page calls ``closeView``. Both
    are raised after the driver quit, like the deadline.
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

        WebDriverWait(driver, 300).until(_signed_in_or_window_gone)
        LOGGER.info("Signed in successfully during shared key flow")

        if vault_sink is not None:
            vault = fetch_vault_entries(driver)
            if vault.cause is VaultCause.WINDOW_CLOSED:
                raise LoginWindowClosed(WINDOW_CLOSED_MESSAGE)
            # Own `try`: an exception from the sink must not reach the outer
            # handler below, which would end the flow with `None` before the
            # unlock page ever opened.
            try:
                vault_sink(vault)
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
                # The `finally` below closes this window. Say so first: a
                # window that vanishes without a word looks like a failure.
                print(
                    "[AuthFlow] Sign-in 2 of 2 finished; this window closes now.",
                    flush=True,
                )
                return shared_key_hex

            if method == "closeView":
                LOGGER.info("closeView invoked; terminating browser session")
                raise SharedKeyCancelledOnPage(CANCELLED_ON_PAGE_MESSAGE)

            LOGGER.debug("Unhandled alert payload: %s", describe_keys(data))

        LOGGER.warning("Shared key flow ended: cause=%s", VaultCause.ALERT_DEADLINE)

    except (LoginWindowClosed, SharedKeyStepAborted):
        raise
    except WebDriverException as err:
        # Closing the window kills the session, and the next command reports
        # it: the sign-in wait, the navigation to the unlock page, the alert
        # wait. A crash arrives through the same types; the classifier keeps it
        # on the old path.
        if describe_lost_session(err) is None:
            LOGGER.exception("Shared key flow terminated unexpectedly")
            return None
        raise LoginWindowClosed(WINDOW_CLOSED_MESSAGE) from err
    except Exception:
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
    """Run the vault-only sign-in; reopen its window at most twice if closed.

    Path (c) of sign-in 2 of 2. A window closed once more than it is reopened
    returns `window_closed`, so the login tool names the window instead of a
    failed request. Never raises otherwise, like the single attempt.
    """

    def _attempt() -> VaultFetchResult:
        result = _vault_entries_flow_once(
            chrome_path=chrome_path, chrome_version=chrome_version
        )
        if result.cause is VaultCause.WINDOW_CLOSED:
            # The restart helper reopens on this type only.
            raise LoginWindowClosed(VaultCause.WINDOW_CLOSED.value)
        return result

    try:
        return restart_on_closed_window(_attempt, step=2, total=2)
    except LoginWindowClosed:
        return VaultFetchResult((), VaultCause.WINDOW_CLOSED)


def _vault_entries_flow_once(
    *, chrome_path: str | None, chrome_version: int | None
) -> VaultFetchResult:
    """Sign in and fetch the vault entries, without opening the unlock page.

    For a cached shared key whose bundle lacks the version field: the user only
    goes through the Google sign-in, the shared key is neither requested nor
    changed. `BrowserPackagesUnusable` passes through like in
    `request_shared_key_flow`; a window the user closed becomes
    `window_closed`, every other failure, including a sign-in that does not
    finish in time, `http_status`. Never raises otherwise.
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
        WebDriverWait(driver, SIGN_IN_TIMEOUT_S).until(_signed_in_or_window_gone)
        LOGGER.info("Signed in successfully during vault entries flow")
        return fetch_vault_entries(driver)
    except Exception as exc:
        # `TimeoutException` when the sign-in does not finish; only the type is
        # logged, the message of a driver error can carry page content. A
        # window the user closed is `window_closed`, so the caller can open it
        # again.
        cause = _failure_cause(exc)
        LOGGER.warning(
            "Vault entries flow ended: cause=%s (%s)", cause, type(exc).__name__
        )
        return VaultFetchResult((), cause)
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
    except SharedKeyStepAborted as err:
        # The user ended the step; the message says which way and what is kept.
        print(f"\n{err}\n", file=sys.stderr)
        return 130
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

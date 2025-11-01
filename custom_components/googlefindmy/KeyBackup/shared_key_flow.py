# custom_components/googlefindmy/KeyBackup/shared_key_flow.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

from custom_components.googlefindmy.KeyBackup.response_parser import (
    get_fmdn_shared_key,
)
from custom_components.googlefindmy.KeyBackup.shared_key_request import (
    get_security_domain_request_url,
)
from custom_components.googlefindmy.chrome_driver import create_driver

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver


LOGGER = logging.getLogger(__name__)


def request_shared_key_flow() -> str | None:
    """Execute the manual shared key retrieval flow via Selenium."""

    driver: WebDriver | None = None
    try:
        driver = create_driver()
    except Exception:  # pragma: no cover - relies on runtime Selenium setup
        LOGGER.exception("Failed to initialize ChromeDriver for shared key flow")
        return None

    try:
        driver.get("https://accounts.google.com/")

        WebDriverWait(driver, 300).until(
            ec.url_contains("https://myaccount.google.com")
        )
        LOGGER.info("Signed in successfully during shared key flow")

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

        while True:
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
                LOGGER.warning("Discarding malformed alert payload: %s", message)
                continue

            method = data.get("method")
            if method == "setVaultSharedKeys":
                vault_keys = data.get("vaultKeys")
                if not isinstance(vault_keys, str):
                    LOGGER.error("Missing or invalid vaultKeys payload: %s", data)
                    continue

                shared_key = get_fmdn_shared_key(vault_keys)
                shared_key_hex: str = shared_key.hex()
                LOGGER.info("Received shared key from authentication flow")
                return shared_key_hex

            if method == "closeView":
                LOGGER.info("closeView invoked; terminating browser session")
                return None

            LOGGER.debug("Unhandled alert payload: %s", data)

    except Exception:  # pragma: no cover - runtime Selenium failures
        LOGGER.exception("Shared key flow terminated unexpectedly")
        return None
    finally:
        if driver is not None:
            driver.quit()


if __name__ == "__main__":
    request_shared_key_flow()

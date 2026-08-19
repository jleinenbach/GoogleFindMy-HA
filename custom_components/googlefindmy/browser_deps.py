# custom_components/googlefindmy/browser_deps.py
"""One place for the "Chrome packages are missing" message.

Selenium and undetected-chromedriver are needed by the manual, user-initiated
credential extraction only. Home Assistant never imports them (no code path it
executes reaches `chrome_driver`), so they are not part of the integration's
`manifest.json` requirements and are therefore *not* installed by Home
Assistant.

That makes a clear failure message part of the deal: a user who runs one of the
command-line helpers in an environment without those packages must be told what
to install, not shown a bare `ModuleNotFoundError`.

Imports nothing beyond the standard library: it is reached from the CLI process.
"""

from __future__ import annotations

import importlib.util
import sys

INSTALL_COMMAND = "pip install selenium undetected-chromedriver"

MISSING_BROWSER_PACKAGES_HINT = (
    "The browser packages for the manual Google login are not installed.\n"
    f"Install them with:\n\n    {INSTALL_COMMAND}\n\n"
    "They are deliberately not part of the Home Assistant requirements of this "
    "integration: Home Assistant never starts a browser, so installing Selenium "
    "into every Home Assistant setup would buy nothing. They are needed only for "
    "the manual credential extraction you are running right now, ideally from a "
    "copy of the integration directory in a flat folder of its own."
)


class BrowserPackagesUnusable(RuntimeError):
    """The browser packages cannot be used, whether absent or broken.

    A type rather than a message, because the message does not survive: the
    driver strategy chain in `chrome_driver.py` ends in its own generic
    "Failed to start ChromeDriver" text, and matching a substring through that
    is what kept failing. A type carries through unchanged.

    "Broken" is not a corner case: `undetected_chromedriver` fails to import
    where `distutils` has been removed from the standard library, which is the
    state of a modern GitHub runner. `find_spec` finds that package, so asking
    whether it is *installed* cannot tell this apart from a working one.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or MISSING_BROWSER_PACKAGES_HINT)


def browser_packages_missing() -> bool:
    """Answer "are the packages there" by looking, not by reading a message.

    Asking whether an error *text* carries the install hint fails wherever the
    hint is translated on its way out, and it is translated twice: the driver
    strategy chain in `chrome_driver.py` ends in a generic "Failed to start
    ChromeDriver" message, and `KeyBackup/shared_key_flow.py` turns any driver
    failure into `None`. Neither leaves a substring to match. The packages
    themselves are still right there to be checked.

    `find_spec` locates without importing, so this costs a path lookup and has
    no side effect on a working installation. It is asked second, though: a
    module already in `sys.modules` is importable by definition, and asking
    `find_spec` about one that was put there without a `__spec__` raises
    `ValueError` — which would report a package that is demonstrably present
    as missing.
    """

    for package in ("selenium", "undetected_chromedriver"):
        if sys.modules.get(package) is not None:
            continue
        try:
            if importlib.util.find_spec(package) is None:
                return True
        except (ImportError, ValueError):
            # A package whose parent is broken cannot be imported either.
            return True
    return False


def missing_browser_dependency(err: ImportError) -> ImportError:
    """Return an ImportError carrying the install hint, chained to the cause."""

    return ImportError(f"{MISSING_BROWSER_PACKAGES_HINT}\n\nOriginal error: {err}")

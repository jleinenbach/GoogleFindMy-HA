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


def missing_browser_dependency(err: ImportError) -> ImportError:
    """Return an ImportError carrying the install hint, chained to the cause."""

    return ImportError(f"{MISSING_BROWSER_PACKAGES_HINT}\n\nOriginal error: {err}")

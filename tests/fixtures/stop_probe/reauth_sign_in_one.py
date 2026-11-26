# tests/fixtures/stop_probe/reauth_sign_in_one.py
"""Start ``--reauth`` and wait in sign-in 1 of 2 until the caller sends SIGTERM.

Sign-in 1 of 2 is replaced by a stand-in that writes ``SIGN_IN_1_WAITING`` to
stderr and then sleeps 30 s, standing in for the Chrome window. Its ``finally``
writes ``BROWSER_CLOSED`` to stderr, standing in for quitting Chrome, so the
caller can see that the stop unwound through it. Everything before the stand-in
is real: the CLI parses ``--reauth`` and clears the cached tokens from the file.
The caller sets ``GOOGLEFINDMY_SECRETS_PATH`` to a file with cached tokens and
puts the repository root on ``PYTHONPATH``.
"""

from __future__ import annotations

import os
import sys
import time

from custom_components.googlefindmy import main as cli_main


def _sign_in_one() -> None:
    """Stand in for the Chrome window of sign-in 1 of 2."""
    try:
        print("SIGN_IN_1_WAITING", file=sys.stderr, flush=True)
        time.sleep(30)
    finally:
        print("BROWSER_CLOSED", file=sys.stderr, flush=True)


def main() -> None:
    """Patch the stand-in in and start the CLI with ``--reauth``.

    The run goes through the real entry point, so the token clearing and the
    restore are the CLI's own; this script only replaces the browser.
    """
    os.environ.pop("GOOGLEFINDMY_ENTRY_ID", None)
    setattr(cli_main, "_ensure_authenticated", _sign_in_one)  # noqa: B010
    cli_main._main(["--reauth"])


if __name__ == "__main__":
    main()

# tests/fixtures/stop_probe/reauth_clear_window.py
"""Start ``--reauth`` and send SIGTERM right after the tokens are cleared.

``--reauth`` clears the cached tokens with one write and only then holds the
callable that puts them back. This probe sends SIGTERM to its own process
straight after that first write, before the CLI can assign the callable, and
writes ``SENDING_SIGTERM`` to stderr just before it does. Only the first write
is followed by the signal: the restore writes through the same function, and a
second signal there would test something else. Sign-in 1 of 2 is replaced by a
stand-in that writes ``SIGN_IN_1_REACHED`` and sleeps; a run that holds the
signal correctly stops before it. The caller sets ``GOOGLEFINDMY_SECRETS_PATH``
to a file with cached tokens and puts the repository root on ``PYTHONPATH``.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from custom_components.googlefindmy import main as cli_main

_real_write = cli_main._atomic_write_json
_writes: list[Path] = []


def _write_then_stop(path: Path, data: dict[str, Any]) -> None:
    """Write as the CLI does; after the first write, stop the process."""
    _real_write(path, data)
    _writes.append(path)
    if len(_writes) == 1:
        print("SENDING_SIGTERM", file=sys.stderr, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)


def _sign_in_one() -> None:
    """Stand in for the Chrome window; a held stop never gets this far."""
    print("SIGN_IN_1_REACHED", file=sys.stderr, flush=True)
    time.sleep(30)


def main() -> None:
    """Patch both stand-ins in and start the CLI with ``--reauth``."""
    os.environ.pop("GOOGLEFINDMY_ENTRY_ID", None)
    setattr(cli_main, "_atomic_write_json", _write_then_stop)  # noqa: B010
    setattr(cli_main, "_ensure_authenticated", _sign_in_one)  # noqa: B010
    cli_main._main(["--reauth"])


if __name__ == "__main__":
    main()

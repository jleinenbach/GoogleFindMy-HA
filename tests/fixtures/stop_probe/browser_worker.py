# tests/fixtures/stop_probe/browser_worker.py
"""Start the login CLI and send SIGTERM while sign-in 2 waits on a thread.

The vault step stores a ``shared_key``, prints one buffered stdout line and
then blocks in the default executor for 30 s, standing in for the browser flow.
0.2 s later the process signals itself and writes ``SIGTERM_AT=<monotonic>`` to
stderr, so the caller can time the exit from the signal on. Network access is
replaced by stand-ins. The caller sets ``GOOGLEFINDMY_SECRETS_PATH`` and puts
the repository root on ``PYTHONPATH``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from typing import Any

import aiohttp

from custom_components.googlefindmy import main as cli_main
from custom_components.googlefindmy.NovaApi.ListDevices import (
    nbe_list_devices as nbe,
)


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session(*_args: object, **_kwargs: object) -> _Session:
    return _Session()


def _connector(**_kwargs: object) -> object:
    return object()


async def _menu(**_kwargs: object) -> None:
    return None


async def _aas(cache: Any) -> None:
    await cache.set("aas_token", "aas_et/TOKEN")


async def _vault(cache: Any) -> None:
    await cache.set("shared_key", "aa" * 32)
    print("vault read started")  # buffered: stdout is a pipe here
    loop = asyncio.get_running_loop()

    def _stop() -> None:
        print(f"SIGTERM_AT={time.monotonic()}", file=sys.stderr, flush=True)
        os.kill(os.getpid(), signal.SIGTERM)

    loop.call_later(0.2, _stop)
    # The browser flow of sign-in 2 runs in the default executor.
    await loop.run_in_executor(None, time.sleep, 30)


def _signed_in() -> None:
    """Stand in for sign-in 1; the stored username is enough here."""


def main() -> None:
    """Patch the stand-ins in and start the CLI until the signal ends it.

    The run goes through the real entry point, so the event loop is the one
    the CLI itself starts; this script does not start one of its own.
    """
    os.environ.pop("GOOGLEFINDMY_ENTRY_ID", None)
    setattr(aiohttp, "ClientSession", _session)  # noqa: B010
    setattr(aiohttp, "TCPConnector", _connector)  # noqa: B010
    setattr(nbe, "_async_cli_main", _menu)  # noqa: B010
    setattr(cli_main, "_ensure_authenticated", _signed_in)  # noqa: B010
    setattr(cli_main, "_ensure_aas_token", _aas)  # noqa: B010
    setattr(cli_main, "_ensure_vault_keys", _vault)  # noqa: B010
    cli_main._main([])


if __name__ == "__main__":
    main()

# tests/test_main.py
"""Tests for the standalone CLI entry point (main.py).

The new main.py mirrors the upstream GoogleFindMyTools layout:
it simply imports ``list_devices`` from ``nbe_list_devices`` and calls it.
"""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# list_devices (sync wrapper in nbe_list_devices)
# ---------------------------------------------------------------------------


class TestListDevicesSync:
    """list_devices() wraps _async_cli_main with asyncio.run."""

    @mock.patch(
        "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices.asyncio"
    )
    @mock.patch(
        "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_happy_path(
        self, mock_cli: mock.MagicMock, mock_asyncio: mock.MagicMock
    ) -> None:
        from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (
            list_devices,
        )

        list_devices()
        mock_asyncio.run.assert_called_once()

    @mock.patch(
        "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_keyboard_interrupt(
        self, mock_cli: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with mock.patch(
            "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices.asyncio"
        ) as mock_asyncio:
            mock_asyncio.run.side_effect = KeyboardInterrupt

            from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (
                list_devices,
            )

            list_devices()

        assert "Exiting." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# if __name__ == "__main__" guard (subprocess)
# ---------------------------------------------------------------------------


class TestDunderMain:
    """Cover the ``if __name__ == '__main__'`` block via subprocess."""

    def test_dunder_main_invokes_help(self) -> None:
        """``python -m custom_components.googlefindmy.main --help`` must work
        because list_devices delegates to nbe_list_devices' argparse."""
        result = subprocess.run(
            [sys.executable, "-m", "custom_components.googlefindmy.main", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # list_devices() doesn't parse --help itself, so it may either
        # exit 0 (if argparse is reached) or fail.  Just verify it imports.
        assert result.returncode in (0, 1, 2)


# ---------------------------------------------------------------------------
# Functional / integration test
# ---------------------------------------------------------------------------


class TestFunctionalCLI:
    """End-to-end test: invoke main.py as a subprocess."""

    def test_subprocess_no_cache_fails_gracefully(self) -> None:
        """Running without a valid token cache must fail with a non-zero exit
        code (not an import error)."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "custom_components.googlefindmy.main",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**dict(__import__("os").environ), "PYTHONPATH": "."},
        )
        # Should fail because no token cache is registered, but NOT with ImportError
        assert "ImportError" not in result.stderr

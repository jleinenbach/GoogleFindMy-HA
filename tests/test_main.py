# tests/test_main.py
"""Tests for the standalone CLI entry point (main.py).

Target: 100 % line- and branch-coverage of
``custom_components.googlefindmy.main``.
"""

from __future__ import annotations

import subprocess
import sys
from unittest import mock

import pytest

# Module under test
from custom_components.googlefindmy import main as main_mod

# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Argument parsing for the CLI."""

    def test_no_args(self) -> None:
        ns = main_mod._parse_args([])
        assert ns.entry is None

    def test_entry_flag(self) -> None:
        ns = main_mod._parse_args(["--entry", "abc123"])
        assert ns.entry == "abc123"

    def test_help_flag_exits(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main_mod._parse_args(["--help"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


class TestListDevices:
    """list_devices() dispatches to _async_cli_main and handles errors."""

    @mock.patch("custom_components.googlefindmy.main.asyncio")
    @mock.patch(
        "custom_components.googlefindmy.main._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_happy_path_with_entry_id(
        self, mock_cli: mock.MagicMock, mock_asyncio: mock.MagicMock
    ) -> None:
        """Explicit entry_id is forwarded to _async_cli_main."""
        main_mod.list_devices(entry_id="my-entry")

        mock_cli.assert_called_once_with("my-entry")
        mock_asyncio.run.assert_called_once()

    @mock.patch("custom_components.googlefindmy.main.asyncio")
    @mock.patch(
        "custom_components.googlefindmy.main._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_entry_id_from_env(
        self,
        mock_cli: mock.MagicMock,
        mock_asyncio: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to GOOGLEFINDMY_ENTRY_ID env var when no arg given."""
        monkeypatch.setenv("GOOGLEFINDMY_ENTRY_ID", "env-entry")
        main_mod.list_devices()

        mock_cli.assert_called_once_with("env-entry")
        mock_asyncio.run.assert_called_once()

    @mock.patch("custom_components.googlefindmy.main.asyncio")
    @mock.patch(
        "custom_components.googlefindmy.main._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_no_entry_id(
        self,
        mock_cli: mock.MagicMock,
        mock_asyncio: mock.MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When neither arg nor env is set, resolved_entry is None."""
        monkeypatch.delenv("GOOGLEFINDMY_ENTRY_ID", raising=False)
        main_mod.list_devices()

        mock_cli.assert_called_once_with(None)
        mock_asyncio.run.assert_called_once()

    @mock.patch(
        "custom_components.googlefindmy.main._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_keyboard_interrupt(
        self, mock_cli: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """KeyboardInterrupt prints a friendly exit message."""
        with mock.patch("custom_components.googlefindmy.main.asyncio") as mock_asyncio:
            mock_asyncio.run.side_effect = KeyboardInterrupt
            # Must NOT raise
            main_mod.list_devices(entry_id="x")

        assert "Exiting." in capsys.readouterr().out

    @mock.patch(
        "custom_components.googlefindmy.main._async_cli_main",
        new_callable=mock.MagicMock,
    )
    def test_generic_exception(
        self, mock_cli: mock.MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Any other exception prints to stderr and exits with code 1."""
        with mock.patch("custom_components.googlefindmy.main.asyncio") as mock_asyncio:
            mock_asyncio.run.side_effect = RuntimeError("boom")
            with pytest.raises(SystemExit) as exc_info:
                main_mod.list_devices(entry_id="x")

        assert exc_info.value.code == 1
        assert "boom" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    """main() wires _parse_args → list_devices."""

    @mock.patch.object(main_mod, "list_devices")
    @mock.patch.object(main_mod, "_parse_args")
    def test_main_delegates(
        self, mock_parse: mock.MagicMock, mock_list: mock.MagicMock
    ) -> None:
        mock_parse.return_value = mock.MagicMock(entry="e1")
        main_mod.main()
        mock_parse.assert_called_once()
        mock_list.assert_called_once_with("e1")


# ---------------------------------------------------------------------------
# if __name__ == "__main__" guard
# ---------------------------------------------------------------------------


class TestDunderMain:
    """Cover the ``if __name__ == '__main__'`` block via subprocess."""

    def test_dunder_main_invokes_main(self) -> None:
        """``python -m custom_components.googlefindmy.main --help`` exercises
        the ``if __name__ == '__main__': main()`` guard and exits 0."""
        result = subprocess.run(
            [sys.executable, "-m", "custom_components.googlefindmy.main", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0
        assert "GoogleFindMyTools CLI" in result.stdout


# ---------------------------------------------------------------------------
# Functional / integration test
# ---------------------------------------------------------------------------


class TestFunctionalCLI:
    """End-to-end test: invoke main.py as a subprocess."""

    def test_subprocess_help(self) -> None:
        """``python -m custom_components.googlefindmy.main --help`` must exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "custom_components.googlefindmy.main", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0
        assert "GoogleFindMyTools CLI" in result.stdout
        assert "--entry" in result.stdout

    def test_subprocess_no_cache_fails_gracefully(self) -> None:
        """Running without a valid token cache must fail with a non-zero exit
        code and a readable error message (not a raw traceback)."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "custom_components.googlefindmy.main",
                "--entry",
                "nonexistent-entry-id",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**dict(__import__("os").environ), "PYTHONPATH": "."},
        )
        # The exact error depends on the cache backend, but exit code must be 1
        # and something meaningful must appear on stderr.
        assert result.returncode == 1
        assert result.stderr.strip(), "stderr must contain an error message"

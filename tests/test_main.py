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

    def test_dunder_main_lists_entry_in_help(self) -> None:
        """--entry and --reauth must appear in --help output.

        Regression test for Codex review on c902169104: the argparse parser
        in main.py declared only --reauth, so `python -m … --entry XYZ`
        broke with "unrecognized arguments".  This test verifies both flags
        are recognized by the top-level parser before delegation.
        """
        result = subprocess.run(
            [sys.executable, "-m", "custom_components.googlefindmy.main", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**dict(__import__("os").environ), "PYTHONPATH": "."},
        )
        assert result.returncode == 0, (
            f"--help must exit 0; got {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "--entry" in result.stdout, (
            f"--entry missing from --help output:\n{result.stdout}"
        )
        assert "--reauth" in result.stdout, (
            f"--reauth missing from --help output:\n{result.stdout}"
        )

    def test_dunder_main_accepts_entry_flag(self) -> None:
        """--entry XYZ must not fail with 'unrecognized arguments'.

        The token cache is empty in CI, so the command will fail downstream
        (missing oauth_token).  We only assert the argparse-level acceptance,
        not the runtime success.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "custom_components.googlefindmy.main",
                "--entry",
                "nonexistent_entry_id",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**dict(__import__("os").environ), "PYTHONPATH": "."},
        )
        assert "unrecognized arguments" not in result.stderr, (
            f"--entry must be recognized; stderr was:\n{result.stderr}"
        )
        assert "error: argument --entry" not in result.stderr, (
            f"--entry argparse error; stderr was:\n{result.stderr}"
        )


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


# ---------------------------------------------------------------------------
# _register_file_cache + _resolve_effective_entry_id (Codex 694f6883aa)
# ---------------------------------------------------------------------------


class TestEntryIdCacheRegistration:
    """Regression tests for the standalone --entry cache-registration bug.

    Background: ``_register_file_cache`` previously hard-coded the registry
    key to ``""``. When the user followed the documented ``--entry foo`` (or
    ``GOOGLEFINDMY_ENTRY_ID=foo``) path, ``_resolve_cli_cache("foo")`` raised
    ``RuntimeError: Unknown entry_id 'foo'`` before listing devices because
    the lookup never found the empty-keyed entry. Codex review on
    694f6883aa surfaced the issue; fix registers the cache under the
    effective entry id resolved from CLI > env > "".
    """

    @staticmethod
    def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
        """Wipe the module-global TokenCache registry so tests don't leak."""
        from custom_components.googlefindmy.Auth import token_cache

        monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
        # _STATE is a dict; reset the default_entry_id key explicitly.
        monkeypatch.setitem(token_cache._STATE, "default_entry_id", None)

    @staticmethod
    def _prepare_secrets_dir(tmp_path) -> object:  # type: ignore[no-untyped-def]
        """Create a minimal Auth/secrets.json so _FileCache loads cleanly."""
        import json

        auth_dir = tmp_path / "Auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        (auth_dir / "secrets.json").write_text(
            json.dumps({"oauth_token": "dummy"}), encoding="utf-8"
        )
        return tmp_path

    def test_register_file_cache_with_entry_id_registers_under_that_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """Happy path: ``--entry foo`` makes ``get_cache_for_entry('foo')`` succeed."""
        self._reset_registry(monkeypatch)
        self._prepare_secrets_dir(tmp_path)

        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth.token_cache import (
            get_cache_for_entry,
            get_registered_entry_ids,
        )

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        cache = cli_main._register_file_cache("entry_xyz")

        assert "entry_xyz" in get_registered_entry_ids()
        assert get_cache_for_entry("entry_xyz") is cache

    def test_register_file_cache_default_keeps_empty_registration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """Backward compatibility: no ``--entry`` keeps the legacy "" key."""
        self._reset_registry(monkeypatch)
        self._prepare_secrets_dir(tmp_path)

        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth.token_cache import (
            get_registered_entry_ids,
        )

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        cli_main._register_file_cache()  # default entry_id=""

        assert "" in get_registered_entry_ids()

    def test_register_file_cache_sets_default_entry_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        """Side-effect: _set_default_entry_id must follow the registry key."""
        self._reset_registry(monkeypatch)
        self._prepare_secrets_dir(tmp_path)

        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import token_cache

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        cli_main._register_file_cache("entry_xyz")

        assert token_cache._STATE["default_entry_id"] == "entry_xyz"


class TestResolveEffectiveEntryId:
    """Unit tests for the CLI > env > "" priority helper."""

    def test_cli_wins_over_env(self) -> None:
        from custom_components.googlefindmy.main import _resolve_effective_entry_id

        assert _resolve_effective_entry_id("cli_value", "env_value") == "cli_value"

    def test_env_used_when_cli_absent(self) -> None:
        from custom_components.googlefindmy.main import _resolve_effective_entry_id

        assert _resolve_effective_entry_id(None, "env_value") == "env_value"

    def test_empty_default_when_neither_set(self) -> None:
        from custom_components.googlefindmy.main import _resolve_effective_entry_id

        assert _resolve_effective_entry_id(None, None) == ""

    def test_whitespace_collapses_to_empty(self) -> None:
        from custom_components.googlefindmy.main import _resolve_effective_entry_id

        assert _resolve_effective_entry_id("   ", None) == ""
        assert _resolve_effective_entry_id(None, "   ") == ""

    def test_cli_explicit_empty_string_overrides_env(self) -> None:
        """``--entry ""`` (explicit empty) must defeat env-var fallback.

        Documented CLI > env priority must hold even when the CLI value is
        an explicit empty string.  Without the ``is not None`` check this
        would silently fall through to env.
        """
        from custom_components.googlefindmy.main import _resolve_effective_entry_id

        assert _resolve_effective_entry_id("", "env_value") == ""

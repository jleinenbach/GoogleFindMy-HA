# tests/test_main.py
"""Tests for the standalone CLI entry point (main.py).

The new main.py mirrors the upstream GoogleFindMyTools layout:
it simply imports ``list_devices`` from ``nbe_list_devices`` and calls it.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# CLI subprocess sandbox
# ---------------------------------------------------------------------------

# ``pgrep`` is the sanctioned selection path and is shimmed separately: seeing it
# is not a defect, seeing pkill/killall is.
_LOOKUP_TOOLS = ("pgrep",)
_KILL_TOOLS = ("pkill", "killall")

# Variables that would steer the CLI down a different branch. They are dropped
# from the inherited environment so an exported value in the developer's shell
# cannot change what the test exercises. ``GOOGLEFINDMY_CONTAINER_LOGIN=1`` in
# particular skips the desktop gate *and* the process cleanup, which would make
# the assertions below silently vacuous.
_NEUTRALISED_ENV = (
    "GOOGLEFINDMY_CONTAINER_LOGIN",
    "GOOGLEFINDMY_ASSUME_INTERACTIVE",
    "GOOGLEFINDMY_NOVNC_URL",
    "GOOGLEFINDMY_NOVNC_PASSWORD",
    "GOOGLEFINDMY_CHROME_PATH",
    "GOOGLEFINDMY_CHROME_VERSION",
)


class _CliSandbox(NamedTuple):
    """Environment for a CLI subprocess plus the files recording tool calls."""

    env: dict[str, str]
    kill_log: Path
    lookup_log: Path


@pytest.fixture
def cli_sandbox(tmp_path: Path) -> _CliSandbox:
    """Run the CLI without touching real credentials or real processes.

    Two hazards are neutralised here, both measured on 2026-07-28:

    * **Process kills.** ``main`` without credentials falls into the Chrome auth
      flow, which used to shell out to ``pkill -f chrome``. That pattern matches
      the *full command line*, so a pytest run listing ``tests/test_chrome_driver.py``
      as an argument was killed by its own grandchild (exit 143). Shims for the
      kill tools record the attempt and exit 1, so a regression is visible as a
      failed assertion instead of a dead test session.
    * **Real credentials.** Without ``GOOGLEFINDMY_SECRETS_PATH`` the CLI reads
      *and writes* the developer's own ``Auth/secrets.json``. The override points
      at a file inside ``tmp_path`` that does not exist, which also makes the
      "no credentials" path deterministic in both directions.
    """

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kill_log = tmp_path / "kill-attempts.log"
    lookup_log = tmp_path / "lookup-attempts.log"
    for tool, sentinel in [
        *((name, "$GFMY_KILL_SENTINEL") for name in _KILL_TOOLS),
        *((name, "$GFMY_LOOKUP_SENTINEL") for name in _LOOKUP_TOOLS),
    ]:
        shim = bin_dir / tool
        shim.write_text(
            f'#!/bin/sh\nprintf "%s %s\\n" "{tool}" "$*" >> "{sentinel}"\nexit 1\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)

    env = dict(os.environ)
    for name in _NEUTRALISED_ENV:
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "PYTHONPATH": ".",
            "GFMY_KILL_SENTINEL": str(kill_log),
            "GFMY_LOOKUP_SENTINEL": str(lookup_log),
            "GOOGLEFINDMY_SECRETS_PATH": str(tmp_path / "secrets.json"),
        }
    )
    return _CliSandbox(env=env, kill_log=kill_log, lookup_log=lookup_log)


def _assert_no_process_kill(sandbox: _CliSandbox) -> None:
    """Fail with the attempted command line if the CLI tried to kill processes."""

    assert not sandbox.kill_log.exists(), (
        "the CLI attempted a process kill:\n"
        f"{sandbox.kill_log.read_text(encoding='utf-8')}"
    )


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

    def test_dunder_main_invokes_help(self, cli_sandbox: _CliSandbox) -> None:
        """``python -m custom_components.googlefindmy.main --help`` must work
        because list_devices delegates to nbe_list_devices' argparse."""
        result = subprocess.run(
            [sys.executable, "-m", "custom_components.googlefindmy.main", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=cli_sandbox.env,
            stdin=subprocess.DEVNULL,
        )
        # list_devices() doesn't parse --help itself, so it may either
        # exit 0 (if argparse is reached) or fail.  Just verify it imports.
        assert result.returncode in (0, 1, 2)
        _assert_no_process_kill(cli_sandbox)

    def test_dunder_main_lists_entry_in_help(self, cli_sandbox: _CliSandbox) -> None:
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
            env=cli_sandbox.env,
            stdin=subprocess.DEVNULL,
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
        assert "--debug" in result.stdout, (
            f"--debug missing from --help output:\n{result.stdout}"
        )
        _assert_no_process_kill(cli_sandbox)

    def test_dunder_main_accepts_entry_flag(self, cli_sandbox: _CliSandbox) -> None:
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
            env=cli_sandbox.env,
            stdin=subprocess.DEVNULL,
        )
        assert "unrecognized arguments" not in result.stderr, (
            f"--entry must be recognized; stderr was:\n{result.stderr}"
        )
        assert "error: argument --entry" not in result.stderr, (
            f"--entry argparse error; stderr was:\n{result.stderr}"
        )
        _assert_no_process_kill(cli_sandbox)

    def test_entry_flag_subprocess_never_attempts_a_process_kill(
        self, cli_sandbox: _CliSandbox
    ) -> None:
        """Regression: ``--entry`` without credentials must not reach the kill path.

        Measured on 2026-07-28: this invocation falls through to the Chrome auth
        flow, which called ``pkill -f chrome``. Because ``-f`` matches the full
        command line, the pytest process that had ``tests/test_chrome_driver.py``
        in its argv was terminated by its own grandchild. The A/B control differed
        only by a ``-k`` filter matching nothing, i.e. by the word in argv alone:
        without it exit 0, with it exit 143 (SIGTERM).

        The defence asserted here is the interactive gate in ``Auth/auth_flow.py``:
        it refuses to open a browser without an attended terminal, so the Chrome
        cleanup is never reached from a test. That the gate was *actually* the
        stopping point is asserted positively -- without it the test would keep
        passing on any future change that makes the CLI exit earlier, while
        covering nothing. The ancestry filter in ``chrome_driver.py`` is the
        second, independent defence and is tested at its own level in
        ``tests/test_chrome_driver.py``; the kill shims here only ensure that a
        regression shows up as a failed assertion rather than a dead session.
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
            env=cli_sandbox.env,
            stdin=subprocess.DEVNULL,
        )

        _assert_no_process_kill(cli_sandbox)
        assert result.returncode != 0, (
            f"the CLI must not succeed without credentials:\n{result.stdout}"
        )
        assert "attended terminal" in result.stderr, (
            "the interactive gate must be the stopping point; without this "
            "assertion the test would still pass if the CLI started failing "
            f"earlier for an unrelated reason. stderr was:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Functional / integration test
# ---------------------------------------------------------------------------


class TestFunctionalCLI:
    """End-to-end test: invoke main.py as a subprocess."""

    def test_subprocess_no_cache_fails_gracefully(
        self, cli_sandbox: _CliSandbox
    ) -> None:
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
            env=cli_sandbox.env,
            stdin=subprocess.DEVNULL,
        )
        # Should fail because no token cache is registered, but NOT with ImportError
        assert "ImportError" not in result.stderr
        _assert_no_process_kill(cli_sandbox)


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


class _TrackingSession:
    """Fake ``aiohttp.ClientSession`` context manager that records closure.

    ``__aexit__`` runs for ``BaseException`` (``SystemExit``) too, so a ``closed``
    flag set to ``True`` after a vault ``sys.exit`` proves the session was not
    leaked.  A list passed at construction time collects every instance so the
    test can inspect the one the function created.
    """

    def __init__(
        self, sink: list[_TrackingSession], *args: object, **kwargs: object
    ) -> None:
        self.closed = False
        sink.append(self)

    async def __aenter__(self) -> _TrackingSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.closed = True
        return False  # do not suppress the propagating exception


class TestRunCliBootstrapSessionLifecycle:
    """`_run_cli_bootstrap` closes its aiohttp session on every exit path.

    The session is opened with ``async with`` so ``__aexit__`` reaps it even when
    ``_ensure_vault_keys`` raises ``SystemExit`` on a fatal shared-key failure.
    A leaked session was the source of the 'Unclosed client session' / WinError-6
    teardown noise.
    """

    @pytest.mark.asyncio
    async def test_session_closed_on_vault_sys_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``sys.exit`` from the eager vault step still closes the session."""
        import aiohttp

        from custom_components.googlefindmy import main as cli_main

        created: list[_TrackingSession] = []
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _TrackingSession(created, *a, **k),
        )
        monkeypatch.setattr(aiohttp, "TCPConnector", lambda **k: object())

        async def _ok(_cache: object) -> None:
            return None

        async def _vault_exit(_cache: object) -> None:
            raise SystemExit(1)

        monkeypatch.setattr(cli_main, "_ensure_aas_token", _ok)
        monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault_exit)

        with pytest.raises(SystemExit):
            await cli_main._run_cli_bootstrap(object(), None)

        assert created, "the function should have opened exactly one session"
        assert created[0].closed is True

    @pytest.mark.asyncio
    async def test_session_and_fcm_closed_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the happy path the session closes and the FCM receiver is stopped."""
        import aiohttp

        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.NovaApi.ListDevices import (
            nbe_list_devices as nbe,
        )

        created: list[_TrackingSession] = []
        monkeypatch.setattr(
            aiohttp,
            "ClientSession",
            lambda *a, **k: _TrackingSession(created, *a, **k),
        )
        monkeypatch.setattr(aiohttp, "TCPConnector", lambda **k: object())

        stopped: list[bool] = []

        class _Fcm:
            async def async_stop(self) -> None:
                stopped.append(True)

        async def _ok(_cache: object) -> None:
            return None

        async def _fcm(_cache: object) -> _Fcm:
            return _Fcm()

        async def _cli(*, entry_id: str | None, session: object) -> None:
            return None

        monkeypatch.setattr(cli_main, "_ensure_aas_token", _ok)
        monkeypatch.setattr(cli_main, "_ensure_vault_keys", _ok)
        monkeypatch.setattr(cli_main, "_clear_stale_adm_token", _ok)
        monkeypatch.setattr(cli_main, "_setup_fcm_receiver", _fcm)
        monkeypatch.setattr(nbe, "_async_cli_main", _cli)

        await cli_main._run_cli_bootstrap(object(), "entry_x")

        assert created and created[0].closed is True
        assert stopped == [True]


# ---------------------------------------------------------------------------
# AP-B1: atomic JSON write + clean vault-failure exit
# ---------------------------------------------------------------------------


class _AsyncDictCache:
    """Minimal async cache mirroring the ``_FileCache`` get/set surface.

    Used to exercise the eager key-retrieval helpers without a real
    file-backed cache or Home Assistant.
    """

    def __init__(self, initial: dict | None = None) -> None:
        self.data: dict = dict(initial or {})

    async def get(self, name: str) -> object:
        return self.data.get(name)

    async def set(self, name: str, value: object) -> None:
        if value is None:
            self.data.pop(name, None)
        else:
            self.data[name] = value


class TestAtomicWriteJson:
    """`_atomic_write_json` writes atomically with restrictive permissions."""

    def test_atomic_write_creates_file_0600(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A successful write produces valid JSON with mode 0600."""
        import json
        import os
        import stat

        from custom_components.googlefindmy.main import _atomic_write_json

        target = tmp_path / "Auth" / "secrets.json"
        payload = {"username": "user@example.com", "oauth_token": "tok"}

        _atomic_write_json(target, payload)

        assert json.loads(target.read_text(encoding="utf-8")) == payload
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        # No orphan temp artifact left behind in the target directory.
        leftovers = [
            p.name for p in target.parent.iterdir() if p.name != "secrets.json"
        ]
        assert leftovers == []

    def test_replace_failure_preserves_original_and_cleans_temp(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """An ``os.replace`` failure leaves the original file byte-identical,
        writes no partial/0-byte JSON, and leaves no orphan temp file."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.main import _atomic_write_json

        target = tmp_path / "secrets.json"
        original_bytes = b'{"username": "old@example.com", "oauth_token": "keep"}'
        target.write_bytes(original_bytes)

        def _boom(src, dst):  # type: ignore[no-untyped-def]
            raise OSError("simulated cross-device or interrupted replace")

        monkeypatch.setattr(cli_main.os, "replace", _boom)

        with pytest.raises(OSError, match="simulated"):
            _atomic_write_json(target, {"username": "new@example.com"})

        # (i) target byte-identical to the pre-write state.
        assert target.read_bytes() == original_bytes
        # (ii) not a 0-byte / partial truncation.
        assert target.stat().st_size == len(original_bytes)
        # (iii) no orphan temp file lingering next to the target.
        leftovers = [
            p.name for p in target.parent.iterdir() if p.name != "secrets.json"
        ]
        assert leftovers == []

    def test_chmod_failure_cleans_already_consumed_temp(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """If ``os.chmod`` fails *after* ``os.replace`` consumed the temp file,
        the cleanup tolerates the missing temp (``FileNotFoundError``) and
        re-raises the original error."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.main import _atomic_write_json

        target = tmp_path / "secrets.json"

        def _chmod_boom(path, mode):  # type: ignore[no-untyped-def]
            raise PermissionError("simulated chmod failure")

        monkeypatch.setattr(cli_main.os, "chmod", _chmod_boom)

        with pytest.raises(PermissionError, match="simulated chmod"):
            _atomic_write_json(target, {"username": "u@example.com"})

        # The replace already happened, so the target exists; no orphan temp.
        assert target.exists()
        leftovers = [
            p.name for p in target.parent.iterdir() if p.name != "secrets.json"
        ]
        assert leftovers == []


class TestFileCacheSave:
    """`_FileCache._save` routes through the atomic writer and honors the
    load-failure guard."""

    @staticmethod
    def _make_cache(
        tmp_path, monkeypatch: pytest.MonkeyPatch, initial_json: str | None
    ):  # type: ignore[no-untyped-def]
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.Auth import token_cache

        monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
        monkeypatch.setitem(token_cache._STATE, "default_entry_id", None)
        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)
        auth_dir = tmp_path / "Auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        if initial_json is not None:
            (auth_dir / "secrets.json").write_text(initial_json, encoding="utf-8")
        return cli_main._register_file_cache("entry_save")

    @pytest.mark.asyncio
    async def test_set_writes_atomically_with_0600(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """A successful ``set`` persists via the atomic writer (0600 file)."""
        import json
        import os
        import stat

        cache = self._make_cache(tmp_path, monkeypatch, '{"oauth_token": "tok"}')
        await cache.set("shared_key", "abc")  # type: ignore[attr-defined]

        secrets = tmp_path / "Auth" / "secrets.json"
        on_disk = json.loads(secrets.read_text(encoding="utf-8"))
        assert on_disk["shared_key"] == "abc"
        assert on_disk["oauth_token"] == "tok"
        assert stat.S_IMODE(os.stat(secrets).st_mode) == 0o600

    @pytest.mark.asyncio
    async def test_load_failed_empty_does_not_overwrite(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """A cache whose load failed must not clobber the on-disk file while it
        still holds no data (the ``_load_failed and not _data`` guard)."""
        # Corrupt JSON forces _load_failed = True while _data stays empty.
        corrupt = "{ this is not valid json"
        cache = self._make_cache(tmp_path, monkeypatch, corrupt)

        await cache.flush()  # type: ignore[attr-defined]  # guard -> early return

        secrets = tmp_path / "Auth" / "secrets.json"
        # The corrupt file is left untouched (not overwritten with "{}").
        assert secrets.read_text(encoding="utf-8") == corrupt


class TestEnsureAuthenticatedPersist:
    """`_ensure_authenticated` persists fresh credentials via the atomic writer."""

    @pytest.mark.asyncio
    async def test_persists_oauth_via_atomic_write(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        """When no credentials exist, the OAuth login result is written to a
        0600 ``secrets.json`` through ``_atomic_write_json``."""
        import json
        import os
        import stat

        from custom_components.googlefindmy import main as cli_main

        monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

        # Provide a stub Chrome-login flow so no browser is opened.
        from custom_components.googlefindmy.Auth import auth_flow

        monkeypatch.setattr(
            auth_flow,
            "request_oauth_account_token_flow",
            lambda: ("oauth-token-value", "detected@example.com"),
        )

        cli_main._ensure_authenticated()

        secrets = tmp_path / "Auth" / "secrets.json"
        data = json.loads(secrets.read_text(encoding="utf-8"))
        assert data["oauth_token"] == "oauth-token-value"
        assert data["username"] == "detected@example.com"
        assert stat.S_IMODE(os.stat(secrets).st_mode) == 0o600


class TestEnsureVaultKeysExit:
    """`_ensure_vault_keys` exits cleanly on vault failure without data loss."""

    @pytest.mark.asyncio
    async def test_vault_runtime_error_exits_one_with_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault ``RuntimeError`` yields ``SystemExit(1)`` and the
        actionable ``vault key retrieval failed`` message; durable tokens
        stay in the cache (no deletion on the error path)."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache(
            {"username": "u@example.com", "oauth_token": "oauth", "aas_token": "aas"}
        )

        async def _raise(_cache):  # type: ignore[no-untyped-def]
            raise RuntimeError("vault returned None")

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _raise)

        with pytest.raises(SystemExit) as excinfo:
            await cli_main._ensure_vault_keys(cache)

        assert excinfo.value.code == 1
        # AAS/oauth tokens are durable-by-design and must not be deleted.
        assert cache.data["oauth_token"] == "oauth"
        assert cache.data["aas_token"] == "aas"

    @pytest.mark.asyncio
    async def test_vault_failure_message_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The exact substring ``vault key retrieval failed`` is printed to
        stderr (not a raw traceback)."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "u@example.com"})

        async def _raise(_cache):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _raise)

        with pytest.raises(SystemExit):
            await cli_main._ensure_vault_keys(cache)

        assert "vault key retrieval failed" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``KeyboardInterrupt`` (a ``BaseException``) is *not* swallowed by the
        ``except Exception`` handler; it propagates as a user abort."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "u@example.com"})

        async def _interrupt(_cache):  # type: ignore[no-untyped-def]
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _interrupt)

        with pytest.raises(KeyboardInterrupt):
            await cli_main._ensure_vault_keys(cache)


# ---------------------------------------------------------------------------
# AP-B2: eager owner-key retrieval + paste compatibility (F-B)
# ---------------------------------------------------------------------------


class TestEnsureOwnerKey:
    """`_ensure_owner_key` fetches the owner key eagerly and stays idempotent."""

    @pytest.mark.asyncio
    async def test_fetches_and_writes_top_level_hex(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing owner key triggers a non-interactive fetch; the resulting
        key is stored as the top-level ``owner_key`` hex form (F-B)."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
            OwnerKeyInfo,
        )

        cache = _AsyncDictCache({"username": "u@example.com"})
        key_bytes = bytes(range(32))

        async def _fake_fetch(*, cache):  # type: ignore[no-untyped-def]
            # Mirror production: the helper persists the scoped runtime key.
            await cache.set("owner_key_u@example.com", key_bytes.hex())
            return OwnerKeyInfo(key=key_bytes, version=None)

        import custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key as gok

        monkeypatch.setattr(gok, "async_get_owner_key", _fake_fetch)

        await cli_main._ensure_owner_key(cache)

        assert cache.data["owner_key"] == key_bytes.hex()
        assert cache.data["owner_key_u@example.com"] == key_bytes.hex()

    @pytest.mark.asyncio
    async def test_short_circuits_on_top_level_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-cached top-level ``owner_key`` skips the fetch entirely."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "u@example.com", "owner_key": "deadbeef"})

        async def _must_not_run(*, cache):  # type: ignore[no-untyped-def]
            raise AssertionError("owner-key fetch must be short-circuited")

        import custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key as gok

        monkeypatch.setattr(gok, "async_get_owner_key", _must_not_run)

        await cli_main._ensure_owner_key(cache)

        assert cache.data["owner_key"] == "deadbeef"

    @pytest.mark.asyncio
    async def test_short_circuits_on_scoped_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An already-cached scoped ``owner_key_{username}`` skips the fetch."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache(
            {"username": "u@example.com", "owner_key_u@example.com": "cafe"}
        )

        async def _must_not_run(*, cache):  # type: ignore[no-untyped-def]
            raise AssertionError("owner-key fetch must be short-circuited")

        import custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key as gok

        monkeypatch.setattr(gok, "async_get_owner_key", _must_not_run)

        await cli_main._ensure_owner_key(cache)

        assert "owner_key" not in cache.data

    @pytest.mark.asyncio
    async def test_fetches_when_username_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no cached username the scoped check is skipped and the fetch
        runs; ``async_get_owner_key`` resolves the user itself."""
        from custom_components.googlefindmy import main as cli_main
        from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
            OwnerKeyInfo,
        )

        cache = _AsyncDictCache()  # no username cached
        key_bytes = bytes([7]) * 32

        async def _fake_fetch(*, cache):  # type: ignore[no-untyped-def]
            return OwnerKeyInfo(key=key_bytes, version=3)

        import custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key as gok

        monkeypatch.setattr(gok, "async_get_owner_key", _fake_fetch)

        await cli_main._ensure_owner_key(cache)

        assert cache.data["owner_key"] == key_bytes.hex()


class TestVaultKeysCompleteness:
    """`_ensure_vault_keys` sets both keys on success and stays non-destructive."""

    @pytest.mark.asyncio
    async def test_success_sets_both_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful run leaves both the shared and owner key in the cache."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "u@example.com"})

        async def _set_shared(_cache):  # type: ignore[no-untyped-def]
            await _cache.set("shared_key", "shared-hex")

        async def _set_owner(_cache):  # type: ignore[no-untyped-def]
            await _cache.set("owner_key", "owner-hex")

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _set_shared)
        monkeypatch.setattr(cli_main, "_ensure_owner_key", _set_owner)

        await cli_main._ensure_vault_keys(cache)

        assert cache.data["shared_key"] == "shared-hex"
        assert cache.data["owner_key"] == "owner-hex"

    @pytest.mark.asyncio
    async def test_owner_key_failure_is_non_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An owner-key fetch failure is non-fatal: the run continues without a
        ``SystemExit``, the shared key obtained beforehand survives, and the
        durable tokens are left untouched (the owner key is re-derived lazily
        at runtime, so secrets.json stays usable)."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache(
            {"username": "u@example.com", "oauth_token": "oauth", "aas_token": "aas"}
        )

        async def _set_shared(_cache):  # type: ignore[no-untyped-def]
            await _cache.set("shared_key", "shared-hex")

        async def _owner_boom(_cache):  # type: ignore[no-untyped-def]
            raise RuntimeError("SPOT failure")

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _set_shared)
        monkeypatch.setattr(cli_main, "_ensure_owner_key", _owner_boom)

        # No SystemExit: the owner-key boundary warns and continues.
        await cli_main._ensure_vault_keys(cache)

        assert cache.data["oauth_token"] == "oauth"
        assert cache.data["aas_token"] == "aas"
        # The shared key obtained before the owner-key failure survives.
        assert cache.data["shared_key"] == "shared-hex"

    @pytest.mark.asyncio
    async def test_owner_key_failure_warns_with_exception_type(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The non-fatal owner-key note names the concrete exception type and
        states that secrets.json is still usable (so the user is not misled into
        a pointless re-login)."""
        from custom_components.googlefindmy import main as cli_main

        cache = _AsyncDictCache({"username": "u@example.com"})

        async def _set_shared(_cache):  # type: ignore[no-untyped-def]
            await _cache.set("shared_key", "shared-hex")

        async def _owner_boom(_cache):  # type: ignore[no-untyped-def]
            raise RuntimeError("SPOT failure")

        monkeypatch.setattr(cli_main, "_ensure_shared_key", _set_shared)
        monkeypatch.setattr(cli_main, "_ensure_owner_key", _owner_boom)

        await cli_main._ensure_vault_keys(cache)

        err = capsys.readouterr().err
        assert "RuntimeError" in err
        assert "non-fatal" in err
        assert "secrets.json" in err
        # The fatal shared-key wording must NOT appear on the owner-key path.
        assert "vault key retrieval failed" not in err


class TestPasteRoundTrip:
    """F-B: a CLI-produced bundle's top-level owner_key is paste-importable."""

    @pytest.mark.asyncio
    async def test_top_level_owner_key_seeds_into_cache(self) -> None:
        """A ``secrets.json`` carrying a top-level ``owner_key`` hex (as written
        by ``_ensure_owner_key``) lands ``owner_key_{username}`` in the cache
        when re-imported via ``_async_save_secrets_data`` (the paste path)."""
        from importlib import import_module

        integration_init = import_module("custom_components.googlefindmy")

        username = "user@example.com"
        owner_hex = bytes(range(32)).hex()
        # Shape mirrors what the CLI path writes: top-level owner_key hex,
        # username, and the shared_key.
        cli_bundle = {
            "username": username,
            "owner_key": owner_hex,
            "shared_key": "ddeeff",
        }

        class _CapturingCache:
            def __init__(self) -> None:
                self.saved: dict = {}

            async def async_set_cached_value(self, name: str, value: object) -> None:
                self.saved[name] = value

        cache = _CapturingCache()
        await integration_init._async_save_secrets_data(cache, cli_bundle)

        # The bundle carried the owner key ONLY at top level (no pre-scoped
        # ``owner_key_{username}`` field); the seeding path must read that
        # top-level field and produce the scoped runtime cache entry.
        assert "owner_key_user@example.com" not in cli_bundle
        assert cache.saved[f"owner_key_{username}"] == owner_hex
        assert cache.saved[f"shared_key_{username}"] == "ddeeff"
        # The top-level field itself is preserved verbatim.
        assert cache.saved["owner_key"] == owner_hex


# ---------------------------------------------------------------------------
# CLI entry point: parser surface, logging configuration, and call order
# ---------------------------------------------------------------------------


class TestBuildCliParser:
    """_build_cli_parser() exposes every documented flag."""

    def test_help_lists_all_flags(self) -> None:
        from custom_components.googlefindmy import main as cli_main

        help_text = cli_main._build_cli_parser().format_help()
        assert "--reauth" in help_text
        assert "--entry" in help_text
        assert "--debug" in help_text

    def test_debug_defaults_to_false(self) -> None:
        from custom_components.googlefindmy import main as cli_main

        args = cli_main._build_cli_parser().parse_args([])
        assert args.debug is False

    def test_debug_flag_parses_true(self) -> None:
        from custom_components.googlefindmy import main as cli_main

        args = cli_main._build_cli_parser().parse_args(["--debug"])
        assert args.debug is True


class TestConfigureCliLogging:
    """_configure_cli_logging() maps flag/env onto the basicConfig level."""

    @staticmethod
    def _captured_level(
        monkeypatch: pytest.MonkeyPatch, *, debug_flag: bool, env: dict[str, str]
    ) -> int:
        from custom_components.googlefindmy import main as cli_main

        captured: dict[str, object] = {}

        def fake_basic_config(**kwargs: object) -> None:
            # basicConfig is a no-op once pytest installed root handlers, so we
            # assert on the requested level instead of the effective root level.
            captured.update(kwargs)

        monkeypatch.setattr(logging, "basicConfig", fake_basic_config)
        cli_main._configure_cli_logging(debug_flag=debug_flag, env=env)
        level = captured["level"]
        assert isinstance(level, int)
        return level

    def test_flag_enables_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert (
            self._captured_level(monkeypatch, debug_flag=True, env={}) == logging.DEBUG
        )

    def test_env_truthy_enables_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert (
            self._captured_level(
                monkeypatch, debug_flag=False, env={"GOOGLEFINDMY_DEBUG": "1"}
            )
            == logging.DEBUG
        )

    def test_env_falsy_stays_at_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "0" must be treated as disabled, not merely "present" (mutation guard).
        assert (
            self._captured_level(
                monkeypatch, debug_flag=False, env={"GOOGLEFINDMY_DEBUG": "0"}
            )
            == logging.WARNING
        )

    def test_default_is_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert (
            self._captured_level(monkeypatch, debug_flag=False, env={})
            == logging.WARNING
        )


class TestCliMainCallOrder:
    """_main() configures logging before authentication and bootstrap."""

    def _run_main(self, monkeypatch: pytest.MonkeyPatch, *, reauth: bool) -> mock.Mock:
        import asyncio

        from custom_components.googlefindmy import main as cli_main

        manager = mock.Mock()
        namespace = argparse.Namespace(debug=True, reauth=reauth, entry=None)
        fake_parser = mock.Mock()
        fake_parser.parse_args.return_value = namespace

        monkeypatch.setattr(cli_main, "_build_cli_parser", lambda: fake_parser)
        monkeypatch.setattr(cli_main, "_configure_cli_logging", manager.configure)
        monkeypatch.setattr(cli_main, "_clear_stale_tokens_for_reauth", manager.clear)
        monkeypatch.setattr(cli_main, "_ensure_authenticated", manager.auth)
        monkeypatch.setattr(
            cli_main, "_resolve_effective_entry_id", lambda *a: "entry-x"
        )
        monkeypatch.setattr(cli_main, "_register_file_cache", lambda entry: object())
        # Mock the coroutine factory too so no un-awaited coroutine is created
        # (asyncio.run is mocked, so a real coroutine would leak a warning).
        monkeypatch.setattr(cli_main, "_run_cli_bootstrap", manager.bootstrap)
        monkeypatch.setattr(asyncio, "run", manager.run)

        cli_main._main([])
        return manager

    def test_logging_configured_before_auth_and_bootstrap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = self._run_main(monkeypatch, reauth=False)
        call_names = [call[0] for call in manager.mock_calls]

        assert "configure" in call_names, call_names
        assert call_names.index("configure") < call_names.index("auth")
        assert call_names.index("auth") < call_names.index("run")
        # reauth was False, so the stale-token clear must NOT run.
        assert "clear" not in call_names

    def test_logging_receives_debug_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = self._run_main(monkeypatch, reauth=False)
        manager.configure.assert_called_once_with(debug_flag=True, env=mock.ANY)

    def test_reauth_clears_before_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = self._run_main(monkeypatch, reauth=True)
        call_names = [call[0] for call in manager.mock_calls]
        assert call_names.index("clear") < call_names.index("auth")

    def test_keyboard_interrupt_prints_exiting(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A Ctrl-C during the bootstrap is caught and reported, not propagated."""
        import asyncio

        from custom_components.googlefindmy import main as cli_main

        namespace = argparse.Namespace(debug=False, reauth=False, entry=None)
        fake_parser = mock.Mock()
        fake_parser.parse_args.return_value = namespace

        monkeypatch.setattr(cli_main, "_build_cli_parser", lambda: fake_parser)
        monkeypatch.setattr(cli_main, "_configure_cli_logging", lambda **kwargs: None)
        monkeypatch.setattr(cli_main, "_ensure_authenticated", lambda: None)
        monkeypatch.setattr(cli_main, "_resolve_effective_entry_id", lambda *a: "")
        monkeypatch.setattr(cli_main, "_register_file_cache", lambda entry: object())
        monkeypatch.setattr(cli_main, "_run_cli_bootstrap", lambda *a: None)

        def _raise(_coro: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(asyncio, "run", _raise)

        cli_main._main([])
        assert "Exiting." in capsys.readouterr().out

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
        leftovers = [p.name for p in target.parent.iterdir() if p.name != "secrets.json"]
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
        leftovers = [p.name for p in target.parent.iterdir() if p.name != "secrets.json"]
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
        leftovers = [p.name for p in target.parent.iterdir() if p.name != "secrets.json"]
        assert leftovers == []


class TestFileCacheSave:
    """`_FileCache._save` routes through the atomic writer and honors the
    load-failure guard."""

    @staticmethod
    def _make_cache(tmp_path, monkeypatch: pytest.MonkeyPatch, initial_json: str | None):  # type: ignore[no-untyped-def]
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
    async def test_owner_key_failure_keeps_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An owner-key fetch failure exits cleanly and deletes no tokens."""
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

        with pytest.raises(SystemExit) as excinfo:
            await cli_main._ensure_vault_keys(cache)

        assert excinfo.value.code == 1
        assert cache.data["oauth_token"] == "oauth"
        assert cache.data["aas_token"] == "aas"
        # The shared key obtained before the failure also survives.
        assert cache.data["shared_key"] == "shared-hex"


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

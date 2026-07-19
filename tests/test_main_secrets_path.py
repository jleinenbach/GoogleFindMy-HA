# tests/test_main_secrets_path.py
"""Tests for standalone secrets.json path resolution.

``_resolve_secrets_path`` lets a container persist the standalone cache on a
dedicated writable volume via ``GOOGLEFINDMY_SECRETS_PATH`` while the integration
code stays on a read-only mount. These tests pin the override and the default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy import main


def test_default_path_is_auth_secrets_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the env override the path is Auth/secrets.json next to main.py."""

    monkeypatch.delenv("GOOGLEFINDMY_SECRETS_PATH", raising=False)

    assert main._resolve_secrets_path() == main._this_dir / "Auth" / "secrets.json"


def test_env_override_redirects_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOOGLEFINDMY_SECRETS_PATH redirects the cache to an explicit file path."""

    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", "/data/secrets.json")

    assert main._resolve_secrets_path() == Path("/data/secrets.json")


def test_blank_env_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only override is ignored and the default path is used."""

    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", "   ")

    assert main._resolve_secrets_path() == main._this_dir / "Auth" / "secrets.json"


def test_env_override_expands_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``~`` in the override is expanded to the user's home directory."""

    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", "~/gfmy/secrets.json")

    resolved = main._resolve_secrets_path()

    assert resolved == Path("~/gfmy/secrets.json").expanduser()
    assert "~" not in str(resolved)


class TestCallSitesHonorOverride:
    """Regression: every secrets.json call site routes through the override.

    Codex review (PR #1208, round 1, finding 4) showed the standalone CLI
    hard-wired ``Auth/secrets.json`` next to the module, which collides with a
    read-only code mount plus atomic writes inside the login container. The fix
    routes ``_register_file_cache``, ``_ensure_authenticated`` and
    ``_clear_stale_tokens_for_reauth`` through ``_resolve_secrets_path()``.

    These tests fail if any single call site is reverted to the module default:
    the default (``_this_dir/Auth/secrets.json``) is pointed at an *empty* code
    directory while ``GOOGLEFINDMY_SECRETS_PATH`` carries the real file, so a
    revert would read/write the wrong (empty) location and break the assertion.
    """

    @staticmethod
    def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
        """Wipe the module-global TokenCache registry so tests don't leak."""
        from custom_components.googlefindmy.Auth import token_cache

        monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
        monkeypatch.setitem(token_cache._STATE, "default_entry_id", None)

    def test_ensure_authenticated_reads_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid credentials at the override path short-circuit the Chrome flow,
        even though the default code dir holds no credentials at all."""
        default_dir = tmp_path / "code"
        default_dir.mkdir()
        monkeypatch.setattr(main, "_this_dir", default_dir, raising=True)

        override = tmp_path / "data" / "secrets.json"
        override.parent.mkdir(parents=True)
        override.write_text(
            json.dumps({"username": "u@example.com", "oauth_token": "tok"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(override))

        from custom_components.googlefindmy.Auth import auth_flow

        def _boom() -> tuple[str, str]:
            raise AssertionError(
                "Chrome flow must not run: override holds valid credentials"
            )

        monkeypatch.setattr(
            auth_flow, "request_oauth_account_token_flow", _boom, raising=True
        )

        main._ensure_authenticated()  # returns via override short-circuit

    def test_ensure_authenticated_writes_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh credentials are persisted to the override path; the default
        Auth dir is never created."""
        default_dir = tmp_path / "code"
        default_dir.mkdir()
        monkeypatch.setattr(main, "_this_dir", default_dir, raising=True)

        override = tmp_path / "data" / "secrets.json"
        monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(override))

        from custom_components.googlefindmy.Auth import auth_flow

        monkeypatch.setattr(
            auth_flow,
            "request_oauth_account_token_flow",
            lambda: ("fresh-oauth", "detected@example.com"),
            raising=True,
        )

        main._ensure_authenticated()

        written = json.loads(override.read_text(encoding="utf-8"))
        assert written["oauth_token"] == "fresh-oauth"
        assert written["username"] == "detected@example.com"
        assert not (default_dir / "Auth" / "secrets.json").exists()

    def test_clear_stale_tokens_honors_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--reauth`` purges stale tokens from the override file; a revert to
        the empty default dir would early-return and leave them in place."""
        default_dir = tmp_path / "code"
        default_dir.mkdir()
        monkeypatch.setattr(main, "_this_dir", default_dir, raising=True)

        override = tmp_path / "data" / "secrets.json"
        override.parent.mkdir(parents=True)
        override.write_text(
            json.dumps(
                {
                    "username": "keep@example.com",
                    "oauth_token": "stale",
                    "aas_token": "stale",
                    "adm_token_abc": "stale",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(override))

        main._clear_stale_tokens_for_reauth()

        remaining = json.loads(override.read_text(encoding="utf-8"))
        assert "oauth_token" not in remaining
        assert "aas_token" not in remaining
        assert "adm_token_abc" not in remaining
        assert remaining["username"] == "keep@example.com"

    def test_register_file_cache_honors_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The file-backed cache loads from the override path; a revert to the
        empty default dir would yield an empty cache (``sync_get`` -> None)."""
        self._reset_registry(monkeypatch)

        default_dir = tmp_path / "code"
        default_dir.mkdir()
        monkeypatch.setattr(main, "_this_dir", default_dir, raising=True)

        override = tmp_path / "data" / "secrets.json"
        override.parent.mkdir(parents=True)
        override.write_text(
            json.dumps({"oauth_token": "marker-value"}), encoding="utf-8"
        )
        monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(override))

        cache: Any = main._register_file_cache("entry_override")

        assert cache.sync_get("oauth_token") == "marker-value"

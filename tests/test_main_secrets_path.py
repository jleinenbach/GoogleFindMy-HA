"""Tests for standalone secrets.json path resolution.

``_resolve_secrets_path`` lets a container persist the standalone cache on a
dedicated writable volume via ``GOOGLEFINDMY_SECRETS_PATH`` while the integration
code stays on a read-only mount. These tests pin the override and the default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.googlefindmy import main


def test_default_path_is_auth_secrets_json() -> None:
    """Without the env override the path is Auth/secrets.json next to main.py."""

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

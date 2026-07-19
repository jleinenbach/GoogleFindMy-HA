# tests/test_main_file_cache.py
"""Behaviour tests for the standalone ``_FileCache`` backing ``secrets.json``.

``_register_file_cache`` builds a duck-typed, file-backed ``TokenCache`` for
standalone (container) mode: ``secrets.json`` is the only store, so the cache
must persist writes atomically, expose the async/sync TokenCache interface, and
*soft-invalidate* the AAS master token (hide it from in-memory reads while
keeping it in the file so it survives a process restart). These paths were
previously unexercised; the tests below pin them so the container credential
lifecycle cannot regress silently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _reset_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe the module-global TokenCache registry so tests don't leak."""
    from custom_components.googlefindmy.Auth import token_cache

    monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
    monkeypatch.setitem(token_cache._STATE, "default_entry_id", None)


def _make_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial: dict[str, object] | str | None = None,
) -> Any:
    """Return a fresh ``_FileCache`` backed by ``tmp_path/Auth/secrets.json``.

    ``initial`` may be a dict (written as JSON), a raw string (written verbatim,
    e.g. to simulate a corrupt file) or ``None`` (no file created).
    """
    from custom_components.googlefindmy import main as cli_main

    _reset_registry(monkeypatch)
    monkeypatch.setattr(cli_main, "_this_dir", tmp_path, raising=True)

    secrets = tmp_path / "Auth" / "secrets.json"
    secrets.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(initial, str):
        secrets.write_text(initial, encoding="utf-8")
    elif initial is not None:
        secrets.write_text(json.dumps(initial), encoding="utf-8")

    return cli_main._register_file_cache("")


def _read(tmp_path: Path) -> dict[str, Any]:
    """Read back the backing secrets.json as a dict."""
    return json.loads((tmp_path / "Auth" / "secrets.json").read_text(encoding="utf-8"))


class TestFileCacheAsyncInterface:
    """The async get/set surface consumed by nbe_list_devices / nova_request."""

    async def test_set_then_get_round_trips_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)

        await cache.set("oauth_token", "abc")

        assert await cache.get("oauth_token") == "abc"
        assert _read(tmp_path)["oauth_token"] == "abc"

    async def test_set_none_pops_normal_key_and_saves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"oauth_token": "abc"})

        await cache.set("oauth_token", None)

        assert await cache.get("oauth_token") is None
        assert "oauth_token" not in _read(tmp_path)

    async def test_aas_token_is_soft_invalidated_but_kept_in_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set('aas_token', None) hides it from reads but keeps it on disk so a
        process restart can recover the master token."""
        cache = _make_cache(tmp_path, monkeypatch, {"aas_token": "master"})

        await cache.set("aas_token", None)

        # Hidden from in-memory reads...
        assert await cache.get("aas_token") is None
        assert await cache.async_get_cached_value("aas_token") is None
        # ...but preserved in the backing file (survives restart, see
        # test_reload_recovers_soft_invalidated_token for the reload proof).
        assert _read(tmp_path)["aas_token"] == "master"

    async def test_reload_recovers_soft_invalidated_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second cache over the same file sees the preserved aas_token."""
        cache = _make_cache(tmp_path, monkeypatch, {"aas_token": "master"})
        await cache.set("aas_token", None)  # soft-invalidate, keep in file

        from custom_components.googlefindmy import main as cli_main

        fresh = cli_main._register_file_cache("")  # same _this_dir/Auth file

        assert await fresh.get("aas_token") == "master"

    async def test_setting_aas_token_value_clears_soft_invalidation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"aas_token": "master"})
        await cache.set("aas_token", None)
        assert await cache.get("aas_token") is None

        await cache.set("aas_token", "new-master")

        assert await cache.get("aas_token") == "new-master"
        assert _read(tmp_path)["aas_token"] == "new-master"

    async def test_async_cached_value_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)

        await cache.async_set_cached_value("k", "v")

        assert await cache.async_get_cached_value("k") == "v"

    async def test_get_or_set_returns_existing_without_generator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"k": "existing"})

        def _fail() -> str:
            raise AssertionError("generator must not run when value exists")

        assert await cache.get_or_set("k", _fail) == "existing"

    async def test_get_or_set_runs_sync_generator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)

        result = await cache.async_get_cached_value_or_set("k", lambda: "made")

        assert result == "made"
        assert _read(tmp_path)["k"] == "made"

    async def test_get_or_set_awaits_coroutine_generator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)

        async def _agen() -> str:
            return "async-made"

        result = await cache.async_get_cached_value_or_set("k", _agen)

        assert result == "async-made"
        assert _read(tmp_path)["k"] == "async-made"

    async def test_all_returns_a_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"a": 1, "b": 2})

        snapshot = await cache.all()
        snapshot["a"] = 999

        assert await cache.get("a") == 1  # mutation of the copy must not leak

    async def test_flush_and_close_persist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)
        cache.sync_set("k", "v")  # sync mutation, then async persist paths

        await cache.flush()
        await cache.close()

        assert _read(tmp_path)["k"] == "v"


class TestFileCacheSyncInterface:
    """The synchronous helpers used outside the event loop."""

    def test_sync_get_and_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch)

        cache.sync_set("k", "v")

        assert cache.sync_get("k") == "v"
        assert _read(tmp_path)["k"] == "v"

    def test_sync_set_none_pops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"k": "v"})

        cache.sync_set("k", None)

        assert cache.sync_get("k") is None
        assert "k" not in _read(tmp_path)

    def test_sync_pop_removes_and_saves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"k": "v"})

        assert cache.sync_pop("k") == "v"
        assert "k" not in _read(tmp_path)

    def test_sync_pop_missing_returns_default_without_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, {"k": "v"})
        before = _read(tmp_path)

        assert cache.sync_pop("absent", "fallback") == "fallback"
        assert _read(tmp_path) == before  # default path performs no save


class TestFileCacheLoadFailureGuard:
    """A corrupt backing file must not be overwritten with empty data."""

    async def test_corrupt_file_not_overwritten_on_empty_flush(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, "{ not valid json")

        await cache.flush()  # _load_failed and no data -> skip save

        assert (tmp_path / "Auth" / "secrets.json").read_text(
            encoding="utf-8"
        ) == "{ not valid json"

    async def test_corrupt_file_is_repaired_once_real_data_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = _make_cache(tmp_path, monkeypatch, "{ not valid json")

        await cache.set("oauth_token", "recovered")

        assert _read(tmp_path)["oauth_token"] == "recovered"

# tests/test_main_cli_single_publish.py
"""The standalone login publishes its credential bundle in one write.

Home Assistant's watcher offers a card whenever ``secrets.json`` changes. The
login used to store the vault keys, the owner-key versions and the push
credentials one by one, so a single run changed the file four times within a
minute and the first versions had no ``shared_key``: the user saw several cards
for one sign-in (L-9b). ``_run_cli_bootstrap`` now runs those steps inside
``_FileCache.deferred_writes()``; the AAS token in front of them is still written
at once, because a later failure must not lose it.

The steps are doubles that write through the cache's public interface, the cache
and its file are real, and ``_atomic_write_json`` is counted, not replaced.
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


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Fcm:
    async def async_stop(self) -> None:
        return None


def _bootstrap_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial: dict[str, Any]
) -> tuple[Any, Path, list[dict[str, Any]]]:
    """Return a real file cache on ``tmp_path`` and the list of written payloads."""
    import aiohttp

    from custom_components.googlefindmy import main as cli_main
    from custom_components.googlefindmy.NovaApi.ListDevices import (
        nbe_list_devices as nbe,
    )

    _reset_registry(monkeypatch)
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setenv("GOOGLEFINDMY_SECRETS_PATH", str(path))

    writes: list[dict[str, Any]] = []
    real_write = cli_main._atomic_write_json

    def _counting_write(target: Path, data: dict[str, Any]) -> None:
        writes.append(json.loads(json.dumps(data)))
        real_write(target, data)

    monkeypatch.setattr(cli_main, "_atomic_write_json", _counting_write)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Session())
    monkeypatch.setattr(aiohttp, "TCPConnector", lambda **k: object())

    async def _cli_main(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nbe, "_async_cli_main", _cli_main)
    cache = cli_main._register_file_cache("")
    return cache, path, writes


def _install_steps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    aas: str = "aas_et/TOKEN",
    shared_key: str = "aa" * 32,
    fcm_credentials: dict[str, Any] | None = None,
    vault_exit: bool = False,
    seen_on_disk_at_vault: list[dict[str, Any]] | None = None,
) -> None:
    from custom_components.googlefindmy import main as cli_main

    async def _aas(cache: Any) -> None:
        await cache.set("aas_token", aas)

    async def _vault(cache: Any) -> None:
        if seen_on_disk_at_vault is not None:
            path = Path(cli_main._resolve_secrets_path())
            seen_on_disk_at_vault.append(json.loads(path.read_text(encoding="utf-8")))
        await cache.set("shared_key", shared_key)
        await cache.set("owner_key", "bb" * 32)
        await cache.set("owner_key_versions", [1])
        if vault_exit:
            raise SystemExit(1)

    async def _clear_adm(cache: Any) -> None:
        cache.sync_pop("adm_token_user@example.com")

    async def _fcm(cache: Any) -> _Fcm:
        await cache.set(
            "fcm_credentials", fcm_credentials or {"gcm": {"token": "push-token"}}
        )
        return _Fcm()

    monkeypatch.setattr(cli_main, "_ensure_aas_token", _aas)
    monkeypatch.setattr(cli_main, "_ensure_vault_keys", _vault)
    monkeypatch.setattr(cli_main, "_clear_stale_adm_token", _clear_adm)
    monkeypatch.setattr(cli_main, "_setup_fcm_receiver", _fcm)


@pytest.mark.asyncio
async def test_one_login_run_writes_keys_and_push_data_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keys, versions and push data reach the file in exactly one write."""
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch,
        tmp_path,
        {
            "username": "user@example.com",
            "oauth_token": "oauth2_4/X",
            "adm_token_user@example.com": "stale",
        },
    )
    _install_steps(monkeypatch)

    await cli_main._run_cli_bootstrap(cache, "")

    with_key = [w for w in writes if "shared_key" in w]
    assert len(with_key) == 1, (
        f"the key steps wrote {len(with_key)} times; Home Assistant offers a card "
        "for each write"
    )
    published = with_key[0]
    assert published["fcm_credentials"] == {"gcm": {"token": "push-token"}}
    assert published["owner_key_versions"] == [1]
    assert "adm_token_user@example.com" not in published
    # The AAS token is written before the block, on its own.
    assert len(writes) == 2
    assert writes[0]["aas_token"] == "aas_et/TOKEN"
    assert "shared_key" not in writes[0]
    assert json.loads(path.read_text(encoding="utf-8")) == published


@pytest.mark.asyncio
async def test_an_unchanged_bundle_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run that stores what is already there leaves the file untouched.

    The watcher compares modification times; a rewrite with the same content
    would still look like a new bundle.
    """
    from custom_components.googlefindmy import main as cli_main

    complete = {
        "username": "user@example.com",
        "aas_token": "aas_et/TOKEN",
        "shared_key": "aa" * 32,
        "owner_key": "bb" * 32,
        "owner_key_versions": [1],
        "fcm_credentials": {"gcm": {"token": "push-token"}},
    }
    cache, path, writes = _bootstrap_world(monkeypatch, tmp_path, complete)
    _install_steps(monkeypatch)

    async def _aas_cached(_cache: Any) -> None:
        return None  # the token is cached; the real step returns early

    monkeypatch.setattr(cli_main, "_ensure_aas_token", _aas_cached)
    before = path.stat().st_mtime_ns

    await cli_main._run_cli_bootstrap(cache, "")

    assert writes == []
    assert path.stat().st_mtime_ns == before


@pytest.mark.asyncio
async def test_an_abort_after_the_token_keeps_the_token_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The AAS token is on disk before the key steps start, and stays there.

    It is what the next run, and the --reauth restore, read from the file. A
    failed key step still persists what it stored before failing: the block
    writes on SystemExit too.
    """
    from custom_components.googlefindmy import main as cli_main

    cache, path, writes = _bootstrap_world(
        monkeypatch, tmp_path, {"username": "user@example.com", "oauth_token": "o"}
    )
    on_disk: list[dict[str, Any]] = []
    _install_steps(monkeypatch, vault_exit=True, seen_on_disk_at_vault=on_disk)

    with pytest.raises(SystemExit):
        await cli_main._run_cli_bootstrap(cache, "")

    assert on_disk and on_disk[0].get("aas_token") == "aas_et/TOKEN"
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["aas_token"] == "aas_et/TOKEN"
    assert final["shared_key"] == "aa" * 32
    assert "fcm_credentials" not in final
    assert len(writes) == 2


def test_nested_blocks_write_once_at_the_outermost_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An inner block does not publish early; only the outermost one writes."""
    cache, _path, writes = _bootstrap_world(monkeypatch, tmp_path, {})

    with cache.deferred_writes():
        cache.sync_set("a", 1)
        with cache.deferred_writes():
            cache.sync_set("b", 2)
        assert writes == []
        cache.sync_set("c", 3)
    assert writes == [{"a": 1, "b": 2, "c": 3}]

    cache.sync_set("d", 4)
    assert len(writes) == 2  # outside the block every change writes again


def test_a_block_that_restores_the_content_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Changing a value and putting it back is no change for the watcher."""
    cache, _path, writes = _bootstrap_world(monkeypatch, tmp_path, {"a": 1})

    with cache.deferred_writes():
        cache.sync_set("a", 2)
        cache.sync_set("a", 1)
    assert writes == []


def test_a_value_that_cannot_be_compared_still_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a comparable snapshot the block writes rather than guess."""
    from custom_components.googlefindmy import main as cli_main

    cache, _path, _writes = _bootstrap_world(monkeypatch, tmp_path, {"a": 1})
    calls: list[str] = []
    monkeypatch.setattr(
        cli_main, "_atomic_write_json", lambda _p, data: calls.append(sorted(data))
    )

    with cache.deferred_writes():
        cache.sync_set("blob", object())
    assert calls == [["a", "blob"]]


def test_a_cache_without_the_block_is_left_as_it_is() -> None:
    """Other caches get a no-op block instead of an AttributeError."""
    from custom_components.googlefindmy import main as cli_main

    with cli_main._deferred_writes(object()):
        pass

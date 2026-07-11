# tests/test_token_cache_contracts.py
"""Contract tests for the TokenCache facade, registry and default resolution.

Covers ``custom_components/googlefindmy/Auth/token_cache.py``:

* ``TokenCache.__init__`` validation guards (entry_id type/emptiness, missing
  event loop).
* ``get_or_set`` per-key lock (thundering-herd protection): the generator runs
  at most once even under concurrent callers; coroutine generators are awaited.
* Sync facades (``get_cached_value`` / ``set_cached_value`` /
  ``get_cached_value_or_set``): the event-loop guard (RuntimeError when called
  from inside a running loop) and the no-instance / with-instance branches.
* ``_get_default_cache`` resolution order (provider, default entry, single
  instance, ambiguity / absence RuntimeErrors).
* Registry helpers (``get_cache_for_entry``, ``_set_default_entry_id``).

Module-global registry state (``_INSTANCES`` / ``_STATE``) is snapshotted and
restored around every test so ordering cannot leak state.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth import token_cache as tc
from custom_components.googlefindmy.Auth.token_cache import TokenCache


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-global registry/state around each test."""
    monkeypatch.setattr(tc, "_INSTANCES", {}, raising=True)
    monkeypatch.setattr(
        tc,
        "_STATE",
        {"legacy_migration_done": False, "default_entry_id": None},
        raising=True,
    )


def _fake_cache(
    entry_id: str, data: dict[str, object] | None = None
) -> SimpleNamespace:
    """A lightweight stand-in registered directly into ``_INSTANCES``."""
    return SimpleNamespace(entry_id=entry_id, _data=dict(data or {}))


def _real_cache(monkeypatch: pytest.MonkeyPatch, entry_id: str = "e1") -> TokenCache:
    """Build a real TokenCache with the Store patched out (no disk I/O)."""
    monkeypatch.setattr(tc, "Store", Mock())
    hass = SimpleNamespace(loop=asyncio.get_event_loop(), data={})
    cache = TokenCache(hass, entry_id)
    cache._store = Mock()  # async_delay_save / async_save are no-ops
    return cache


# --------------------------------------------------------------------------- #
# __init__ validation guards                                                   #
# --------------------------------------------------------------------------- #


def test_init_rejects_non_string_entry_id() -> None:
    with pytest.raises(TypeError):
        TokenCache(SimpleNamespace(loop=object()), 123)  # type: ignore[arg-type]


def test_init_rejects_empty_entry_id() -> None:
    with pytest.raises(ValueError):
        TokenCache(SimpleNamespace(loop=object()), "   ")


def test_init_requires_event_loop() -> None:
    with pytest.raises(RuntimeError):
        TokenCache(SimpleNamespace(loop=None), "e1")


# --------------------------------------------------------------------------- #
# get_or_set — thundering-herd per-key lock                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_or_set_returns_existing_without_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _real_cache(monkeypatch)
    cache._data["k"] = "cached"
    gen = Mock()
    assert await cache.get_or_set("k", gen) == "cached"
    gen.assert_not_called()


@pytest.mark.asyncio
async def test_get_or_set_awaits_coroutine_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _real_cache(monkeypatch)

    async def gen() -> str:
        return "computed"

    assert await cache.get_or_set("k", gen) == "computed"
    assert cache._data["k"] == "computed"


@pytest.mark.asyncio
async def test_get_or_set_runs_generator_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent callers must not all run the generator (per-key lock)."""
    cache = _real_cache(monkeypatch)
    calls = 0

    async def gen() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)  # yield so a second caller can enter the lock
        return "once"

    results = await asyncio.gather(
        cache.get_or_set("k", gen),
        cache.get_or_set("k", gen),
        cache.get_or_set("k", gen),
    )
    assert results == ["once", "once", "once"]
    assert calls == 1


# --------------------------------------------------------------------------- #
# set() after close                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_after_close_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _real_cache(monkeypatch)
    await cache.close()
    with pytest.raises(RuntimeError):
        await cache.set("k", "v")


# --------------------------------------------------------------------------- #
# _get_default_cache — resolution order                                        #
# --------------------------------------------------------------------------- #


def test_default_cache_raises_when_no_instances() -> None:
    with pytest.raises(RuntimeError, match="No TokenCache registered"):
        tc._get_default_cache()


def test_default_cache_returns_single_instance() -> None:
    cache = _fake_cache("only")
    tc._INSTANCES["only"] = cache
    assert tc._get_default_cache() is cache


def test_default_cache_uses_default_entry_id() -> None:
    a, b = _fake_cache("a"), _fake_cache("b")
    tc._INSTANCES.update({"a": a, "b": b})
    tc._STATE["default_entry_id"] = "b"
    assert tc._get_default_cache() is b


def test_default_cache_ambiguous_without_default_raises() -> None:
    tc._INSTANCES.update({"a": _fake_cache("a"), "b": _fake_cache("b")})
    with pytest.raises(RuntimeError, match="Multiple config entries"):
        tc._get_default_cache()


def test_default_cache_prefers_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache resolved via the nova_request provider wins over the registry."""
    from custom_components.googlefindmy.NovaApi import nova_request

    provided = _fake_cache("provided")
    monkeypatch.setattr(nova_request, "resolve_cache_from_provider", lambda: provided)
    tc._INSTANCES["registry"] = _fake_cache("registry")
    assert tc._get_default_cache() is provided


# --------------------------------------------------------------------------- #
# Registry helpers                                                             #
# --------------------------------------------------------------------------- #


def test_get_cache_for_entry_hit_and_miss() -> None:
    cache = _fake_cache("e1")
    tc._INSTANCES["e1"] = cache
    assert tc.get_cache_for_entry("e1") is cache
    with pytest.raises(KeyError):
        tc.get_cache_for_entry("missing")


def test_set_default_entry_id_single_and_force() -> None:
    tc._INSTANCES["e1"] = _fake_cache("e1")
    tc._set_default_entry_id("e1")
    assert tc._STATE["default_entry_id"] == "e1"
    tc._set_default_entry_id("forced", force=True)
    assert tc._STATE["default_entry_id"] == "forced"


def test_set_default_entry_id_ambiguous_clears_default() -> None:
    tc._INSTANCES.update({"a": _fake_cache("a"), "b": _fake_cache("b")})
    tc._set_default_entry_id("a")  # >1 instances, not previously default -> None
    assert tc._STATE["default_entry_id"] is None


# --------------------------------------------------------------------------- #
# Sync facades — event-loop guard + instance branches                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sync_getter_rejects_running_loop() -> None:
    with pytest.raises(RuntimeError, match="inside event loop"):
        tc.get_cached_value("k")


@pytest.mark.asyncio
async def test_sync_setter_rejects_running_loop() -> None:
    with pytest.raises(RuntimeError, match="inside event loop"):
        tc.set_cached_value("k", "v")


@pytest.mark.asyncio
async def test_sync_get_or_set_rejects_running_loop() -> None:
    with pytest.raises(RuntimeError, match="inside event loop"):
        tc.get_cached_value_or_set("k", lambda: "v")


def test_sync_getter_returns_none_without_instances() -> None:
    assert tc.get_cached_value("k") is None


def test_sync_getter_reads_instance_data() -> None:
    tc._INSTANCES["e1"] = _fake_cache("e1", {"k": "value"})
    assert tc.get_cached_value("k") == "value"


def test_sync_setter_writes_and_deletes_instance_data() -> None:
    cache = _fake_cache("e1")
    tc._INSTANCES["e1"] = cache
    tc.set_cached_value("k", "v")
    assert cache._data["k"] == "v"
    tc.set_cached_value("k", None)  # None removes the key
    assert "k" not in cache._data


def test_sync_setter_without_instances_is_noop() -> None:
    tc.set_cached_value("k", "v")  # only logs a warning, no raise


def test_sync_get_or_set_without_instances_calls_generator() -> None:
    assert tc.get_cached_value_or_set("k", lambda: "computed") == "computed"


def test_sync_get_or_set_hit_and_store() -> None:
    cache = _fake_cache("e1", {"present": "hit"})
    tc._INSTANCES["e1"] = cache
    assert tc.get_cached_value_or_set("present", lambda: "new") == "hit"
    assert tc.get_cached_value_or_set("absent", lambda: "new") == "new"
    assert cache._data["absent"] == "new"

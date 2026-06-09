# tests/test_spot_token_retrieval.py
"""Coverage + regression-lock tests for ``Auth/spot_token_retrieval``.

These tests exercise the *real* orchestration of ``async_get_spot_token`` and
``_async_generate_spot_token`` by mocking only their **dependencies**
(``async_request_token``, ``request_token``, ``async_get_aas_token``,
``async_get_username``) — never the SUT functions themselves. Mocking the SUT
away is exactly the anti-pattern that left this module at 23 % coverage despite
an existing propagation test (see ``test_spot_and_nova_cache_propagation.py``,
which monkeypatches ``async_get_spot_token`` as a whole).

Each ``RL-*`` test pins one member of the 6-commit "entry-scoped cache
propagation" bug family so a regression would fail loudly:

    RL-1  async happy path forwards ``cache=`` AND ``aas_provider=``   (bfd4a4f1)
    RL-2  sync fallback forwards ``cache=`` AND resolved ``aas_token=`` (9b78b80a)
    RL-3  without a provider, AAS is resolved from the SAME cache       (bfd4a4f1)
    RL-4  ``cache=None`` raises ValueError (multi-account safety)       (1dd738e6)
    RL-5  ``username=None`` resolved via provider; empty -> RuntimeError
    RL-6  empty token from sync fallback -> RuntimeError
    RL-7  cache key ``spot_token_{username}`` + ``get_or_set`` caching
    RL-8  caller-supplied ``aas_provider`` is honored, not overwritten
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.Auth import (
    spot_token_retrieval as spot_module,
)

pytestmark = pytest.mark.asyncio


class _FakeCache:
    """Minimal entry-scoped cache mirroring ``TokenCache.get_or_set`` semantics.

    Tracks how often the generator runs so cache-hit vs. cache-miss is testable.
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.generator_calls = 0

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        if value is None:
            self._data.pop(key, None)
            return
        self._data[key] = value

    async def get_or_set(
        self, name: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        if (existing := self._data.get(name)) is not None:
            return existing
        self.generator_calls += 1
        candidate = generator()
        if hasattr(candidate, "__await__"):
            candidate = await candidate
        self._data[name] = candidate
        return candidate


def _patch_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    async_token: str | None = "async-token",
    sync_token: str = "sync-token",
    aas_token: str = "aas-default",
    username: str | None = "user@example.com",
) -> dict[str, Any]:
    """Patch the four module-level dependencies; return the mocks for assertions."""

    m_async_request = AsyncMock(return_value=async_token)
    m_request = MagicMock(return_value=sync_token)
    m_async_aas = AsyncMock(return_value=aas_token)
    m_async_username = AsyncMock(return_value=username)

    monkeypatch.setattr(spot_module, "async_request_token", m_async_request)
    monkeypatch.setattr(spot_module, "request_token", m_request)
    monkeypatch.setattr(spot_module, "async_get_aas_token", m_async_aas)
    monkeypatch.setattr(spot_module, "async_get_username", m_async_username)

    return {
        "async_request_token": m_async_request,
        "request_token": m_request,
        "async_get_aas_token": m_async_aas,
        "async_get_username": m_async_username,
    }


# --------------------------------------------------------------------------- #
# RL-1: async happy path forwards cache= AND aas_provider=                      #
# --------------------------------------------------------------------------- #
async def test_rl1_async_path_forwards_cache_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(monkeypatch, async_token="spot-async")

    async def custom_provider() -> str:
        return "provider-aas"

    token = await spot_module.async_get_spot_token(
        "user@example.com", cache=cache, aas_provider=custom_provider
    )

    assert token == "spot-async"
    deps["async_request_token"].assert_awaited_once()
    _, kwargs = deps["async_request_token"].call_args
    assert kwargs["cache"] is cache
    assert kwargs["aas_provider"] is custom_provider
    # Happy path must NOT touch the sync fallback.
    deps["request_token"].assert_not_called()


# --------------------------------------------------------------------------- #
# RL-2: sync fallback forwards cache= AND resolved aas_token=                   #
# --------------------------------------------------------------------------- #
async def test_rl2_sync_fallback_forwards_cache_and_aas_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    # async path returns empty -> sync fallback engaged.
    deps = _patch_deps(monkeypatch, async_token="", sync_token="spot-sync")

    async def custom_provider() -> str:
        return "resolved-aas"

    token = await spot_module.async_get_spot_token(
        "user@example.com", cache=cache, aas_provider=custom_provider
    )

    assert token == "spot-sync"
    deps["request_token"].assert_called_once()
    _, kwargs = deps["request_token"].call_args
    assert kwargs["cache"] is cache
    assert kwargs["aas_token"] == "resolved-aas"


# --------------------------------------------------------------------------- #
# RL-3: without provider, AAS resolved from the SAME cache                      #
# --------------------------------------------------------------------------- #
async def test_rl3_fallback_aas_uses_same_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(
        monkeypatch, async_token="", sync_token="spot-sync", aas_token="aas-cache"
    )

    token = await spot_module.async_get_spot_token("user@example.com", cache=cache)

    assert token == "spot-sync"
    deps["async_get_aas_token"].assert_awaited_once()
    _, kwargs = deps["async_get_aas_token"].call_args
    assert kwargs["cache"] is cache
    # Resolved AAS token must flow into the sync retriever.
    _, rt_kwargs = deps["request_token"].call_args
    assert rt_kwargs["aas_token"] == "aas-cache"


# --------------------------------------------------------------------------- #
# RL-4: cache=None -> ValueError (multi-account safety)                         #
# --------------------------------------------------------------------------- #
async def test_rl4_cache_none_raises_value_error() -> None:
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        await spot_module.async_get_spot_token("user@example.com", cache=None)


# --------------------------------------------------------------------------- #
# RL-5: username resolution via provider; invalid -> RuntimeError              #
# --------------------------------------------------------------------------- #
async def test_rl5_username_resolved_from_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(
        monkeypatch, async_token="spot-async", username="resolved@example.com"
    )

    token = await spot_module.async_get_spot_token(None, cache=cache)

    assert token == "spot-async"
    deps["async_get_username"].assert_awaited_once()
    _, kwargs = deps["async_get_username"].call_args
    assert kwargs["cache"] is cache
    # Cache key must use the resolved username.
    assert "spot_token_resolved@example.com" in cache._data


async def test_rl5_empty_username_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    _patch_deps(monkeypatch, username=None)

    with pytest.raises(RuntimeError, match="username is not configured"):
        await spot_module.async_get_spot_token(None, cache=cache)


# --------------------------------------------------------------------------- #
# RL-6: empty token from sync fallback -> RuntimeError                          #
# --------------------------------------------------------------------------- #
async def test_rl6_empty_sync_token_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    _patch_deps(monkeypatch, async_token="", sync_token="")

    async def custom_provider() -> str:
        return "resolved-aas"

    with pytest.raises(RuntimeError, match="request_token returned empty token"):
        await spot_module.async_get_spot_token(
            "user@example.com", cache=cache, aas_provider=custom_provider
        )


# --------------------------------------------------------------------------- #
# RL-7: cache key spot_token_{username} + get_or_set caching                    #
# --------------------------------------------------------------------------- #
async def test_rl7_cache_hit_skips_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    cache._data["spot_token_user@example.com"] = "cached-token"
    deps = _patch_deps(monkeypatch)

    token = await spot_module.async_get_spot_token("user@example.com", cache=cache)

    assert token == "cached-token"
    assert cache.generator_calls == 0
    deps["async_request_token"].assert_not_awaited()


async def test_rl7_cache_miss_stores_under_expected_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    _patch_deps(monkeypatch, async_token="fresh-token")

    token = await spot_module.async_get_spot_token("user@example.com", cache=cache)

    assert token == "fresh-token"
    assert cache.generator_calls == 1
    assert cache._data["spot_token_user@example.com"] == "fresh-token"


# --------------------------------------------------------------------------- #
# RL-8: caller-supplied aas_provider honored, not overwritten by fallback       #
# --------------------------------------------------------------------------- #
async def test_rl8_custom_provider_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(monkeypatch, async_token="", sync_token="spot-sync")

    async def custom_provider() -> str:
        return "custom-aas"

    token = await spot_module.async_get_spot_token(
        "user@example.com", cache=cache, aas_provider=custom_provider
    )

    assert token == "spot-sync"
    # The default cache-based AAS resolver must NOT be used when a provider is given.
    deps["async_get_aas_token"].assert_not_awaited()
    _, rt_kwargs = deps["request_token"].call_args
    assert rt_kwargs["aas_token"] == "custom-aas"


# --------------------------------------------------------------------------- #
# Branch completeness: async-immediate vs. sync-fallback dispatch               #
# --------------------------------------------------------------------------- #
async def test_branch_async_immediate_skips_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(monkeypatch, async_token="immediate")

    token = await spot_module.async_get_spot_token("user@example.com", cache=cache)

    assert token == "immediate"
    deps["request_token"].assert_not_called()
    deps["async_get_aas_token"].assert_not_awaited()


async def test_branch_none_async_token_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _FakeCache()
    deps = _patch_deps(monkeypatch, async_token=None, sync_token="from-thread")

    token = await spot_module.async_get_spot_token("user@example.com", cache=cache)

    assert token == "from-thread"
    deps["request_token"].assert_called_once()

"""Regression guard for AAS-token preservation on scoped-token invalidation.

Root cause (v1.7.10): ``_invalidate_token_async`` unconditionally nulled the
quasi-permanent AAS master token in the volatile entry-scoped cache on *every*
scoped (spot/adm) auth failure. Because the runtime AAS regeneration path only
reads the one-time (consumed) Chrome cookie and never falls back to the
persistent ``entry.data`` SSOT, this turned a routinely transient scoped-token
failure into a user-visible reauth that only a full integration reload could
heal. These tests pin the fixed contract: a scoped invalidation must leave the
AAS untouched, while a genuinely non-scoped kind still clears it.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module

DATA_AAS_TOKEN = spot_request_module.DATA_AAS_TOKEN


class _DummyCache(TokenCache):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


@pytest.mark.asyncio
async def test_scoped_spot_invalidation_preserves_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")
    await cache.set("spot_token_user@example.com", "ya29.scoped")

    await spot_request_module._invalidate_token_async(
        "spot", "user@example.com", cache=cache
    )

    # Scoped token cleared, quasi-permanent AAS preserved.
    assert cache._data.get("spot_token_user@example.com") is None
    assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/master"


@pytest.mark.asyncio
async def test_scoped_adm_invalidation_preserves_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")
    await cache.set("adm_token_user@example.com", "ya29.scoped")

    await spot_request_module._invalidate_token_async(
        "adm", "user@example.com", cache=cache
    )

    assert cache._data.get("adm_token_user@example.com") is None
    assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/master"


@pytest.mark.asyncio
async def test_non_scoped_kind_still_clears_aas() -> None:
    cache = _DummyCache()
    await cache.set(DATA_AAS_TOKEN, "aas_et/master")

    await spot_request_module._invalidate_token_async(
        "aas", "user@example.com", cache=cache
    )

    # A non-scoped kind falls through and clears the AAS (defensive path).
    assert cache._data.get(DATA_AAS_TOKEN) is None

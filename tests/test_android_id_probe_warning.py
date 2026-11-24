# tests/test_android_id_probe_warning.py
"""No warning about a generated android_id while a bundle is only probed.

Validating a discovered bundle runs the token exchange against an in-memory
cache that is thrown away afterwards. Generating an android_id there is
expected and nothing is stored, so the warning "Generated new android_id" is
noise. A cache that persists keeps the warning, because there a missing
identifier is worth knowing about.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from custom_components.googlefindmy.api import _EphemeralCache
from custom_components.googlefindmy.Auth import (
    aas_token_retrieval,
    adm_token_retrieval,
    token_retrieval,
)

_USER = "probe.user@example.com"
_WARNING_TEXT = "Generated new android_id"


class _StoredCache:
    """A cache that keeps what it is given, like the entry's token cache."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(values or {})

    async def get(self, name: str) -> Any:
        return self.values.get(name)

    async def set(self, name: str, value: Any) -> None:
        self.values[name] = value


Resolver = Callable[[Any], Awaitable[int]]


def _aas(cache: Any) -> Awaitable[int]:
    return aas_token_retrieval._get_or_generate_android_id(_USER, cache)


def _adm_entry(cache: Any) -> Awaitable[int]:
    return adm_token_retrieval._resolve_android_id_for_entry(_USER, cache=cache)


def _adm_isolated(cache: Any) -> Awaitable[int]:
    return adm_token_retrieval._resolve_android_id_for_isolated_flow(
        _USER, secrets_bundle=None, cache_get=cache.get, cache_set=cache.set
    )


def _token(cache: Any) -> Awaitable[int]:
    return token_retrieval._resolve_android_id(cache=cache, username=_USER)


_RESOLVERS = pytest.mark.parametrize(
    "resolve",
    [_aas, _adm_entry, _adm_isolated, _token],
    ids=["aas", "adm_entry", "adm_isolated", "token"],
)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and _WARNING_TEXT in record.getMessage()
    ]


@_RESOLVERS
@pytest.mark.asyncio
async def test_probing_a_bundle_logs_no_android_id_warning(
    resolve: Resolver, caplog: pytest.LogCaptureFixture
) -> None:
    """Two probes against a throwaway cache without FCM data: no warning."""

    cache = _EphemeralCache(oauth_token=None, email=_USER)

    with caplog.at_level(logging.DEBUG):
        first = await resolve(cache)
        second = await resolve(cache)

    assert _warnings(caplog) == []
    assert first == second


@_RESOLVERS
@pytest.mark.asyncio
async def test_a_persistent_cache_keeps_the_warning(
    resolve: Resolver, caplog: pytest.LogCaptureFixture
) -> None:
    """Without a stored identifier a persistent cache still warns, once."""

    cache = _StoredCache()

    with caplog.at_level(logging.DEBUG):
        generated = await resolve(cache)

    assert len(_warnings(caplog)) == 1
    assert cache.values[f"android_id_{_USER}"] == generated


@_RESOLVERS
@pytest.mark.asyncio
async def test_a_stored_android_id_stays_unchanged(
    resolve: Resolver, caplog: pytest.LogCaptureFixture
) -> None:
    """An identifier already in the cache is used as it is, without a warning."""

    stored = 0x1234567890ABCDEF
    cache = _StoredCache({f"android_id_{_USER}": stored})

    with caplog.at_level(logging.DEBUG):
        resolved = await resolve(cache)

    assert resolved == stored
    assert cache.values[f"android_id_{_USER}"] == stored
    assert _warnings(caplog) == []

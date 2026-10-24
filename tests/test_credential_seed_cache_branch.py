# tests/test_credential_seed_cache_branch.py
"""The cache branch of the credential seed keeps a fresher FCM registration.

An entry that carries no credentials of its own is seeded from the token
cache (``elif cache_snapshot:`` in ``async_setup_entry``). The cached secrets
bundle then goes through ``_async_save_secrets_data``, which writes every
bundle field to the top level of the cache. Without a filter that included
the bundle's ``fcm_credentials``: an FCM registration the receiver had renewed
since (stored at the top level, the entry has no bundle to update) was
replaced by the bundle's older copy on every setup, which is the regeneration
regression of PR #233 on a path the entry write cannot reach.

The harness replaces ``_async_save_secrets_data`` with a mock. The tests take
the real function before the harness runs and replay the bundle the seed
passed, so the cache shows what the next start would really hold.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_AAS_TOKEN,
    DATA_SECRET_BUNDLE,
)
from tests.test_config_entry_startup import _run_credential_seed

EMAIL = "user@example.com"


def _registration(token: str) -> dict[str, Any]:
    """Return a complete FCM registration carrying ``token``."""
    return {
        "gcm": {"android_id": "1234567890", "security_token": "9876543210"},
        "fcm": {"registration": {"token": token}},
    }


def _token(value: Any) -> Any:
    """Return the FCM token of a cached registration."""
    return value["fcm"]["registration"]["token"]


def _real_save_secrets_data() -> Callable[..., Any]:
    """Return the module's ``_async_save_secrets_data`` as currently bound."""
    integration = importlib.import_module("custom_components.googlefindmy")
    real_save: Callable[..., Any] = integration._async_save_secrets_data
    return real_save


async def _seed_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
    *,
    entry_data: dict[str, Any],
    cached: dict[str, Any],
    real_save: Callable[..., Any] | None = None,
) -> Any:
    """Run the seed, then replay the bundle it passed through the real save.

    ``real_save`` must be taken before the first harness run of a test: the
    harness replaces the module attribute with a mock for the rest of it.
    """
    if real_save is None:
        real_save = _real_save_secrets_data()
    # A replay through the harness mock would assert nothing at all.
    assert not isinstance(real_save, Mock)
    harness = await _run_credential_seed(
        monkeypatch, stub_coordinator_factory, entry_data=entry_data, cached=cached
    )
    harness.integration._async_save_secrets_data.assert_awaited_once()
    passed = harness.integration._async_save_secrets_data.await_args.args[1]
    await real_save(harness.cache, passed)
    return harness


@pytest.mark.asyncio
async def test_the_cache_branch_keeps_a_fresher_cached_registration(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A renewed top-level registration survives the reseed of an old bundle."""
    harness = await _seed_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached={
            DATA_SECRET_BUNDLE: {
                "username": EMAIL,
                "fcm_credentials": _registration("stale"),
            },
            "fcm_credentials": _registration("fresh"),
        },
    )

    assert _token(harness.cache.values["fcm_credentials"]) == "fresh"
    # The rest of the bundle is still seeded.
    assert harness.cache.values["username"] == EMAIL


@pytest.mark.asyncio
async def test_the_cache_branch_seeds_the_bundle_registration_when_none_is_cached(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Without a cached top-level value, the bundle's registration is the seed.

    This is the cold-start gap the recovery exists for (Issue #152): the FCM
    receiver must not start with empty credentials.
    """
    harness = await _seed_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached={
            DATA_SECRET_BUNDLE: {
                "username": EMAIL,
                "fcm_credentials": _registration("bundle"),
            },
        },
    )

    assert _token(harness.cache.values["fcm_credentials"]) == "bundle"


@pytest.mark.parametrize("empty", [{}, None])
@pytest.mark.asyncio
async def test_the_cache_branch_replaces_an_empty_cached_registration(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
    empty: Any,
) -> None:
    """An empty cached value is no registration, so the bundle's one wins."""
    harness = await _seed_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached={
            DATA_SECRET_BUNDLE: {
                "username": EMAIL,
                "fcm_credentials": _registration("bundle"),
            },
            "fcm_credentials": empty,
        },
    )

    assert _token(harness.cache.values["fcm_credentials"]) == "bundle"


@pytest.mark.asyncio
async def test_the_entry_branch_passes_the_entry_bundle_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The entry is the source of truth: its bundle reaches the seed as it is.

    The receiver keeps an entry bundle current itself, so a cached top-level
    value must not take precedence over it.
    """
    entry_bundle = {"username": EMAIL, "fcm_credentials": _registration("entry")}
    harness = await _seed_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL, DATA_SECRET_BUNDLE: entry_bundle},
        cached={"fcm_credentials": _registration("cached")},
    )

    passed = harness.integration._async_save_secrets_data.await_args.args[1]
    assert passed == entry_bundle
    assert _token(harness.cache.values["fcm_credentials"]) == "entry"


@pytest.mark.asyncio
async def test_the_cache_branch_without_a_bundle_does_not_reach_the_bundle_seed(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A cache with only an AAS token takes the manual-credentials path."""
    harness = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached={
            DATA_AAS_TOKEN: "aas_et/CACHED_TOKEN_VALUE",
            "fcm_credentials": _registration("fresh"),
        },
    )

    harness.integration._async_save_secrets_data.assert_not_awaited()
    harness.integration._async_seed_manual_credentials.assert_awaited_once()
    assert _token(harness.cache.values["fcm_credentials"]) == "fresh"


class _ReceiverCacheView:
    """Expose the harness cache through the ``set`` the receiver calls."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    async def set(self, name: str, value: Any) -> None:
        await self._cache.async_set_cached_value(name, value)


class _ConfigEntries:
    """Config-entry manager double that records every entry write."""

    def __init__(self, entry: Any) -> None:
        self._entry = entry
        self.update_calls: list[dict[str, Any]] = []

    def async_get_entry(self, entry_id: str) -> Any | None:
        return self._entry if entry_id == self._entry.entry_id else None

    def async_update_entry(self, entry: Any, **kwargs: Any) -> bool:
        self.update_calls.append(dict(kwargs))
        return True


@pytest.mark.asyncio
async def test_a_regeneration_for_an_entry_without_a_bundle_survives_the_next_setup(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """End to end: renew, then set up again from the cache the receiver wrote."""
    real_save = _real_save_secrets_data()
    first = await _run_credential_seed(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached={
            DATA_SECRET_BUNDLE: {
                "username": EMAIL,
                "fcm_credentials": _registration("stale"),
            },
        },
    )
    manager = _ConfigEntries(first.entry)
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace(config_entries=manager))
    receiver._entry_caches[first.entry.entry_id] = _ReceiverCacheView(first.cache)

    receiver._on_credentials_updated_for_entry(
        first.entry.entry_id, _registration("fresh")
    )
    if receiver._active_tasks:
        await asyncio.gather(*list(receiver._active_tasks))

    # There is no bundle in the entry, so the receiver writes nothing there.
    assert manager.update_calls == []
    assert _token(first.cache.values["fcm_credentials"]) == "fresh"

    second = await _seed_and_replay(
        monkeypatch,
        stub_coordinator_factory,
        entry_data={CONF_GOOGLE_EMAIL: EMAIL},
        cached=dict(first.cache.values),
        real_save=real_save,
    )
    assert second.integration._async_save_secrets_data is not real_save

    assert _token(second.cache.values["fcm_credentials"]) == "fresh"


def test_a_bundle_that_is_not_a_mapping_is_passed_on_unchanged() -> None:
    """The filter leaves a malformed cached bundle to the seed's own handling."""
    integration = importlib.import_module("custom_components.googlefindmy")
    snapshot = {"fcm_credentials": _registration("fresh")}

    assert (
        integration._bundle_without_fields_refreshed_since("not-a-bundle", snapshot)
        == "not-a-bundle"
    )
    assert integration._bundle_without_fields_refreshed_since(
        {"username": EMAIL, "fcm_credentials": _registration("stale")}, snapshot
    ) == {"username": EMAIL}

# tests/test_owner_key_versions_bundle_import.py
"""The bundle field ``owner_key_versions`` through the HA import.

``_async_save_secrets_data`` stores every field a bundle carries, so the field
reaches the ``TokenCache`` without a dedicated branch (L-2). What the import adds
is the removal (L-16, amendment A5): a bundle that carries a ``shared_key`` and no
versions drops versions an earlier import left in the cache; a bundle without a
``shared_key`` leaves them alone.

The cache is a real ``TokenCache`` on the Store stub of ``tests/conftest.py``, so
"the same bytes in the cache" is read back from the class the integration uses.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy as integration_init
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    OWNER_KEY_VERSIONS_FIELD,
    VaultEntry,
    from_bundle_field,
    to_bundle_field,
)
from tests.helpers import owner_key_vault_fixtures as fixtures
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world

_HYGIENE_WINDOW = 16  # AE-14: 16 hex or Base64 characters, at least 8 bytes


async def _new_cache(entry_id: str = "entry-okv") -> TokenCache:
    hass = SimpleNamespace(loop=asyncio.get_running_loop(), data={})
    return await TokenCache.create(hass, entry_id)  # type: ignore[arg-type]


def _bundle(world: VaultWorld, **extra: Any) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "username": "user@example.com",
        "owner_key": world.owner_keys[world.spot_version].hex(),
        "shared_key": world.shared_key.hex(),
    }
    bundle.update(extra)
    return bundle


def _leaked_windows(text: str, world: VaultWorld) -> list[str]:
    hits: list[str] = []
    for form in world.all_secret_strings():
        for start in range(len(form) - _HYGIENE_WINDOW + 1):
            window = form[start : start + _HYGIENE_WINDOW]
            if window in text:
                hits.append(window)
                break
    return hits


def test_the_fixture_and_the_module_name_the_same_field() -> None:
    """Tests seed the field under the fixture's constant; it must be the real one."""

    assert fixtures.OWNER_KEY_VERSIONS_FIELD == OWNER_KEY_VERSIONS_FIELD


def test_the_field_name_is_loggable_and_not_runtime_refreshed() -> None:
    """M3-15: nothing renews the field at run time, so a bundle's copy is final.

    In ``_RUNTIME_REFRESHED_BUNDLE_FIELDS`` the field would be skipped when the
    entry is seeded from the cache's bundle copy. In ``_LOGGABLE_BUNDLE_FIELDS``
    it is, so a failed save names it instead of "other field".
    """

    assert OWNER_KEY_VERSIONS_FIELD not in (
        integration_init._RUNTIME_REFRESHED_BUNDLE_FIELDS
    )
    assert OWNER_KEY_VERSIONS_FIELD in integration_init._LOGGABLE_BUNDLE_FIELDS


@pytest.mark.asyncio
async def test_every_vault_entry_lands_with_the_same_bytes() -> None:
    """L-2: each entry stays encrypted and byte-identical in the TokenCache."""

    world = make_vault_world(versions=(1, 2, 3), spot_version=3)
    cache = await _new_cache()

    await integration_init._async_save_secrets_data(
        cache, _bundle(world, **{OWNER_KEY_VERSIONS_FIELD: world.bundle_field})
    )

    stored = await cache.async_get_cached_value(OWNER_KEY_VERSIONS_FIELD)
    assert stored == world.bundle_field
    entries = from_bundle_field(stored)
    assert entries is not None
    assert {entry.version: entry.key for entry in entries} == (world.wrapped_owner_keys)


@pytest.mark.asyncio
async def test_two_epochs_of_one_version_keep_only_the_higher() -> None:
    """L-2: ``to_bundle_field`` keeps the higher epoch, and the import keeps it."""

    world = make_vault_world(versions=(1, 2), spot_version=2)
    older = VaultEntry(2, 100, "finder_hw", world.wrapped_owner_keys[1])
    newer = VaultEntry(2, 200, "finder_hw", world.wrapped_owner_keys[2])
    first = VaultEntry(1, 50, "finder_hw", world.wrapped_owner_keys[1])
    field = to_bundle_field([first, newer, older])
    cache = await _new_cache()

    await integration_init._async_save_secrets_data(
        cache, _bundle(world, **{OWNER_KEY_VERSIONS_FIELD: field})
    )

    entries = from_bundle_field(
        await cache.async_get_cached_value(OWNER_KEY_VERSIONS_FIELD)
    )
    assert entries is not None
    by_version = {entry.version: entry for entry in entries}
    assert sorted(by_version) == [1, 2]
    assert by_version[2].epoch == 200
    assert by_version[2].key == world.wrapped_owner_keys[2]


@pytest.mark.asyncio
async def test_a_bundle_without_the_field_into_an_empty_cache_adds_nothing() -> None:
    """L-2: without the field the import writes what it wrote before."""

    world = make_vault_world()
    cache = await _new_cache()
    bundle = _bundle(world)

    await integration_init._async_save_secrets_data(cache, bundle)

    snapshot = await cache.all()
    assert OWNER_KEY_VERSIONS_FIELD not in snapshot
    for name, value in bundle.items():
        assert snapshot[name] == value, name


@pytest.mark.asyncio
async def test_a_new_field_replaces_the_old_one_whole() -> None:
    """L-16: a successful fetch replaces the field, never mixes in old entries."""

    old_world = make_vault_world(versions=(1, 2), spot_version=2)
    new_world = make_vault_world(versions=(3,), spot_version=3)
    cache = await _new_cache()
    await cache.async_set_cached_value(OWNER_KEY_VERSIONS_FIELD, old_world.bundle_field)

    await integration_init._async_save_secrets_data(
        cache,
        _bundle(new_world, **{OWNER_KEY_VERSIONS_FIELD: new_world.bundle_field}),
    )

    stored = await cache.async_get_cached_value(OWNER_KEY_VERSIONS_FIELD)
    assert stored == new_world.bundle_field


@pytest.mark.asyncio
async def test_stale_field_removed_by_a_bundle_with_shared_key_and_no_versions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A5: versions of an older shared_key go; the log names only the field."""

    old_world = make_vault_world(versions=(1, 2), spot_version=2)
    new_world = make_vault_world(versions=(1,), spot_version=1)
    cache = await _new_cache()
    await cache.async_set_cached_value(OWNER_KEY_VERSIONS_FIELD, old_world.bundle_field)

    with caplog.at_level(logging.DEBUG, logger=integration_init._LOGGER.name):
        await integration_init._async_save_secrets_data(
            cache, _bundle(new_world), account_label="u***@example.com"
        )

    assert await cache.async_get_cached_value(OWNER_KEY_VERSIONS_FIELD) is None
    assert OWNER_KEY_VERSIONS_FIELD not in await cache.all()
    removal = [
        record.getMessage()
        for record in caplog.records
        if "removed bundle field owner_key_versions" in record.getMessage()
    ]
    assert len(removal) == 1
    assert _leaked_windows(caplog.text, old_world) == []
    assert _leaked_windows(caplog.text, new_world) == []


@pytest.mark.asyncio
async def test_no_field_in_the_cache_means_no_removal_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No field in the cache: no removal and no log line claiming one."""

    world = make_vault_world()
    cache = await _new_cache()

    with caplog.at_level(logging.DEBUG, logger=integration_init._LOGGER.name):
        await integration_init._async_save_secrets_data(cache, _bundle(world))

    assert "owner_key_versions" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shared_key",
    [pytest.param(None, id="absent"), pytest.param("  \n ", id="blank")],
)
async def test_bundle_without_shared_key_keeps_field(shared_key: str | None) -> None:
    """A5: a bundle without a shared_key says nothing about the versions' key."""

    world = make_vault_world(versions=(1, 2), spot_version=2)
    cache = await _new_cache()
    await cache.async_set_cached_value(OWNER_KEY_VERSIONS_FIELD, world.bundle_field)
    bundle = _bundle(world)
    del bundle["shared_key"]
    if shared_key is not None:
        bundle["shared_key"] = shared_key

    await integration_init._async_save_secrets_data(cache, bundle)

    stored = await cache.async_get_cached_value(OWNER_KEY_VERSIONS_FIELD)
    assert stored == world.bundle_field


class _RemovalFailsCache:
    """Holds the field and rejects its removal, as a closed or broken cache would."""

    def __init__(self, field: dict[str, Any]) -> None:
        self.entry_id = "entry-okv"
        self.values: dict[str, Any] = {OWNER_KEY_VERSIONS_FIELD: field}

    async def async_get_cached_value(self, name: str) -> Any:
        return self.values.get(name)

    async def async_set_cached_value(self, name: str, value: Any) -> None:
        if name == OWNER_KEY_VERSIONS_FIELD and value is None:
            raise OSError("store unavailable")
        self.values[name] = value


@pytest.mark.asyncio
async def test_a_failed_removal_is_logged_and_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The import goes on; the warning names the field and no secret."""

    old_world = make_vault_world(versions=(1, 2), spot_version=2)
    new_world = make_vault_world(versions=(1,), spot_version=1)
    cache = _RemovalFailsCache(old_world.bundle_field)

    with caplog.at_level(logging.DEBUG, logger=integration_init._LOGGER.name):
        await integration_init._async_save_secrets_data(
            cache,  # type: ignore[arg-type]
            _bundle(new_world),
        )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "owner_key_versions" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "entry entry-okv" in warnings[0]
    assert _leaked_windows(caplog.text, old_world) == []
    assert _leaked_windows(caplog.text, new_world) == []

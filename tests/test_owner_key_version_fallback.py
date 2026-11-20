# tests/test_owner_key_version_fallback.py
"""Owner-key version gap (issue #246): tracker version ahead of Spot.

A tracker reports ``ownerKeyVersion`` N while Spot only serves the owner key of
version M < N. The planned fallback reads the owner key of version N from the
bundle field ``owner_key_versions`` (vault entries wrapped with the shared key)
before any forced Spot refresh, and otherwise raises a dedicated error class with
a remedy instead of the generic ``DecryptionError`` that ends in account reauth.

These tests pin that contract before the code exists (expected failures). They
deliberately do not import the planned error class; the expectation is written
as behaviour asserts on ``type(exc).__name__`` and ``remedy`` so that the only
reason for red today is an ``AssertionError``. Today's outcome on the base commit
is ``DecryptionError("Identity key decryption failed.")`` after a forced
``async_get_owner_key`` refresh and an ``async_get_eid_info`` metadata call.

All key material comes from ``tests.helpers.owner_key_vault_fixtures``; expected
values are the plaintexts fed into that factory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as _dl,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
)
from tests.helpers import DummyCache
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world

pytestmark = pytest.mark.asyncio

# Planned bundle field name (plan decision AE-1); spelled out here because the
# constant does not exist yet and must not be imported by a red test.
_BUNDLE_FIELD = "owner_key_versions"

_NOT_IMPLEMENTED = "owner-key vault fallback not implemented"

# Spot requests ``ownerKeyVersion = -1`` for "latest"; any other value would be a
# request for a specific version.
_LATEST_VERSION_REQUEST = -1


class _SpotDouble:
    """Spot-side doubles that count every call that would reach Spot.

    Spot calls in the sense of L-3 are ``async_get_owner_key(force_refresh=True)``
    and every ``async_get_eid_info`` call. The non-forced owner-key lookup reads the
    entry cache and is not a Spot call.
    """

    def __init__(self, world: VaultWorld) -> None:
        self._world = world
        self.force_refresh_calls = 0
        self.eid_info_calls = 0
        self.versioned_calls = 0

    @property
    def spot_calls(self) -> int:
        return self.force_refresh_calls + self.eid_info_calls

    def _served(self) -> OwnerKeyInfo:
        version = self._world.spot_version
        return OwnerKeyInfo(key=self._world.owner_keys[version], version=version)

    async def get_owner_key(
        self, *, cache: object, force_refresh: bool = False, **kwargs: Any
    ) -> OwnerKeyInfo:
        if force_refresh:
            self.force_refresh_calls += 1
        if kwargs.get("owner_key_version", _LATEST_VERSION_REQUEST) != (
            _LATEST_VERSION_REQUEST
        ):
            self.versioned_calls += 1
        return self._served()

    async def get_eid_info(self, *, cache: object, **kwargs: Any) -> object:
        self.eid_info_calls += 1
        if kwargs.get("owner_key_version", _LATEST_VERSION_REQUEST) != (
            _LATEST_VERSION_REQUEST
        ):
            self.versioned_calls += 1
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(
                ownerKeyVersion=self._world.spot_version
            )
        )

    async def get_shared_key(self, *, cache: object, **_kwargs: Any) -> bytes:
        return self._world.shared_key


class _CountingCache(DummyCache):
    """Entry cache that counts key-clearing writes (the reauth trigger)."""

    def __init__(self) -> None:
        super().__init__()
        self.key_clears: list[str] = []

    async def set(self, name: str, value: object | None) -> None:
        if value is None and ("owner_key" in name or "shared_key" in name):
            self.key_clears.append(name)
        await super().set(name, value)


def _install(monkeypatch: pytest.MonkeyPatch, world: VaultWorld) -> _SpotDouble:
    spot = _SpotDouble(world)
    monkeypatch.setattr(_dl, "async_get_owner_key", spot.get_owner_key)
    monkeypatch.setattr(_dl, "async_get_eid_info", spot.get_eid_info)
    monkeypatch.setattr(_dl, "async_get_shared_key", spot.get_shared_key)
    return spot


def _cache_with_field(field: dict[str, dict[str, Any]] | None) -> _CountingCache:
    cache = _CountingCache()
    if field is not None:
        cache.values[_BUNDLE_FIELD] = field
    return cache


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_IMPLEMENTED)
async def test_tracker_newer_than_spot_decrypts_from_vault_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-3: entry N present -> EIK from the vault entry, no Spot call, no reauth."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _cache_with_field(world.bundle_field)

    try:
        eiks = await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    except DecryptionError as exc:
        raise AssertionError(f"unexpected {type(exc).__name__}") from exc

    assert spot.spot_calls == 0
    assert spot.versioned_calls == 0
    assert world.eiks[2] in eiks
    assert cache.key_clears == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_IMPLEMENTED)
async def test_tracker_newer_than_spot_without_field_raises_version_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-4: no bundle field -> dedicated error with the login remedy, no reauth."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    cache = _cache_with_field(None)

    with pytest.raises(DecryptionError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )

    assert type(ei.value).__name__ == "OwnerKeyVersionUnavailableError"
    assert getattr(ei.value, "remedy", None) == "login"
    assert cache.key_clears == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_IMPLEMENTED)
async def test_version_missing_in_field_raises_version_unavailable_with_vault_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-4: field present but entry N missing -> dedicated error, vault remedy."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    field_without_n = {k: v for k, v in world.bundle_field.items() if k != "2"}
    cache = _cache_with_field(field_without_n)

    with pytest.raises(DecryptionError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )

    assert type(ei.value).__name__ == "OwnerKeyVersionUnavailableError"
    assert getattr(ei.value, "remedy", None) == "vault"
    assert cache.key_clears == []


async def test_tracker_older_than_spot_keeps_stale_owner_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-3 sentence 2: N < M keeps ``StaleOwnerKeyError``, even with a vault field."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    _install(monkeypatch, world)
    cache = _cache_with_field(world.bundle_field)

    with pytest.raises(StaleOwnerKeyError):
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(1), cache=cache
        )

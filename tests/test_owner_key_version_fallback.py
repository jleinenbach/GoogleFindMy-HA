# tests/test_owner_key_version_fallback.py
"""Owner-key version gap (issue #246): tracker version ahead of Spot.

A tracker reports ``ownerKeyVersion`` N while Spot only serves the owner key of
version M < N. The planned fallback reads the owner key of version N from the
bundle field ``owner_key_versions`` (vault entries wrapped with the shared key)
without any Spot call: neither a forced ``async_get_owner_key`` refresh nor the
``async_get_eid_info`` metadata call. Otherwise it raises a dedicated error class
with a remedy instead of the generic ``DecryptionError``, and that class must stay
out of the account-wide reauth path of the poll verdict.

These tests pin that contract before the code exists (expected failures). They
deliberately do not import the planned error class; the expectation is written
as behaviour asserts on ``type(exc).__name__`` and ``remedy`` so that the only
reason for red today is an ``AssertionError``. Today's outcome on the base commit
is ``DecryptionError("Identity key decryption failed.")`` after a forced
``async_get_owner_key`` refresh and an ``async_get_eid_info`` metadata call, and
the poll verdict treats that error as account-wide.

All key material comes from ``tests.helpers.owner_key_vault_fixtures``; expected
values are the plaintexts fed into that factory.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator.polling import _MAX_DECRYPT_FAILURES
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
from tests.helpers.polling_mixin_stub import PollingStub

pytestmark = pytest.mark.asyncio

_NOT_IMPLEMENTED = "owner-key vault fallback not implemented"
_NOT_CLASSIFIED = "owner-key version gap not yet exempt from account reauth"


class _SpotDouble:
    """Spot-side doubles that count every call that would reach Spot.

    Counted as Spot calls (L-3): ``async_get_owner_key(force_refresh=True)`` and
    every ``async_get_eid_info`` call. A non-forced owner-key lookup returns the
    served key without being counted; that models a warm entry cache, where the
    lookup is a cache read. With a cold cache the production lookup reaches Spot
    as well, which these tests do not cover.

    The signatures mirror the production functions keyword for keyword, without
    ``**kwargs``, so a call with an unknown keyword fails loudly with
    ``TypeError`` instead of being swallowed.
    """

    def __init__(self, world: VaultWorld) -> None:
        self._world = world
        self.force_refresh_calls = 0
        self.eid_info_calls = 0

    @property
    def spot_calls(self) -> int:
        return self.force_refresh_calls + self.eid_info_calls

    async def get_owner_key(
        self,
        *,
        cache: object,
        username: str | None = None,
        eid_info_getter: Callable[[], Awaitable[Any]] | None = None,
        shared_key_getter: Callable[[], Awaitable[bytes]] | None = None,
        force_refresh: bool = False,
    ) -> OwnerKeyInfo:
        if force_refresh:
            self.force_refresh_calls += 1
        version = self._world.spot_version
        return OwnerKeyInfo(key=self._world.owner_keys[version], version=version)

    async def get_eid_info(self, *, cache: object) -> object:
        self.eid_info_calls += 1
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(
                ownerKeyVersion=self._world.spot_version
            )
        )


class _CountingCache(DummyCache):
    """Entry cache that counts key-clearing writes (the blind-refresh trigger)."""

    def __init__(self) -> None:
        super().__init__()
        self.key_clears: list[str] = []

    async def set(self, name: str, value: object | None) -> None:
        if value is None and ("owner_key" in name or "shared_key" in name):
            self.key_clears.append(name)
        await super().set(name, value)


@pytest.fixture(autouse=True)
def _isolate_eik_cache() -> Iterator[None]:
    """Keep the module-global EIK cache from carrying results between tests."""
    _dl.clear_eik_cache()
    yield
    _dl.clear_eik_cache()


def _install(monkeypatch: pytest.MonkeyPatch, world: VaultWorld) -> _SpotDouble:
    spot = _SpotDouble(world)
    monkeypatch.setattr(_dl, "async_get_owner_key", spot.get_owner_key)
    monkeypatch.setattr(_dl, "async_get_eid_info", spot.get_eid_info)
    return spot


def _seeded_cache(
    world: VaultWorld, bundle_field: dict[str, dict[str, Any]] | None
) -> _CountingCache:
    cache = _CountingCache()
    world.seed_entry_cache(cache.values, bundle_field)
    return cache


def _make_polling_stub() -> PollingStub:
    """Return a PollingStub seeded with the decrypt-escalation state fields."""
    stub = PollingStub()
    stub._consecutive_decrypt_failures = 0
    stub._last_decrypt_reauth_monotonic = None
    stub._last_decrypt_error = None
    stub._monotonic = lambda: 1_000_000.0
    stub._set_auth_state = MagicMock()
    return stub


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_IMPLEMENTED)
async def test_tracker_newer_than_spot_decrypts_from_vault_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-3: entry N present -> EIK from the vault entry, no Spot call at all.

    No Spot call means neither a forced owner-key refresh nor the metadata call
    ``async_get_eid_info``; the vault path must return before both. No key is
    cleared from the entry cache.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _seeded_cache(world, world.bundle_field)

    try:
        eiks = await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    except DecryptionError as exc:
        raise AssertionError(f"unexpected {type(exc).__name__}") from exc

    assert spot.spot_calls == 0
    assert world.eiks[2] in eiks
    assert cache.key_clears == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_IMPLEMENTED)
async def test_tracker_newer_than_spot_without_field_raises_version_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-4: no bundle field -> dedicated error with the login remedy.

    No key is cleared from the entry cache. That the error stays out of the
    account reauth is pinned by
    ``test_version_unavailable_error_never_drives_account_reauth``.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    cache = _seeded_cache(world, None)

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
    """L-4: field present but entry N missing -> dedicated error, vault remedy.

    No key is cleared from the entry cache; the reauth verdict is pinned as in the
    field-missing case.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    field_without_n = {k: v for k, v in world.bundle_field.items() if k != "2"}
    cache = _seeded_cache(world, field_without_n)

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
    cache = _seeded_cache(world, world.bundle_field)

    with pytest.raises(StaleOwnerKeyError):
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(1), cache=cache
        )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NOT_CLASSIFIED)
async def test_version_unavailable_error_never_drives_account_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L-4: the gap error never advances the reauth counter and never escalates.

    The error that ``async_retrieve_identity_key`` raises for the field-missing
    case is fed into both entry points of the poll verdict:
    ``note_decrypt_failure`` (poll and push path) and
    ``_decrypt_failure_is_account_wide`` (cycle verdict), with and without a
    sibling device decrypting in the same cycle. A ``DecryptionError`` subclass
    alone is not enough: every subclass except the own-report class defaults to
    the account-wide path, so this stays red until the poll verdict exempts the
    gap error explicitly.
    """
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    cache = _seeded_cache(world, None)

    with pytest.raises(DecryptionError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    gap_error = ei.value

    stub = _make_polling_stub()
    for _ in range(_MAX_DECRYPT_FAILURES + 1):
        assert (
            stub.note_decrypt_failure(stale=False, error=gap_error, device="tracker")
            is False
        )
    assert stub._consecutive_decrypt_failures == 0
    stub._set_auth_state.assert_not_called()

    assert stub._decrypt_failure_is_account_wide(gap_error, True) is False
    assert stub._decrypt_failure_is_account_wide(gap_error, False) is False

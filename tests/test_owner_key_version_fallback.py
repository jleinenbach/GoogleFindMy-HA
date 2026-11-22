# tests/test_owner_key_version_fallback.py
"""Owner-key version gap (issue #246): tracker version ahead of Spot.

A tracker reports ``ownerKeyVersion`` N while Spot only serves the owner key of
version M < N. The planned fallback reads the owner key of version N from the
bundle field ``owner_key_versions`` (vault entries wrapped with the shared key)
without any Spot call: neither a forced ``async_get_owner_key`` refresh nor the
``async_get_eid_info`` metadata call. Otherwise it raises a dedicated error class
with a remedy instead of the generic ``DecryptionError``, and that class must stay
out of the account-wide reauth path of the poll verdict.

The first tests pin that contract as behaviour asserts on
``type(exc).__name__`` and ``remedy``; they were written before the code existed.
``test_version_unavailable_error_never_drives_account_reauth`` stays an expected
failure until the poll verdict exempts the gap error. The remaining tests pin the
vault lookup, the per-account refresh throttle and the gap register in
``get_owner_key``.

All key material comes from ``tests.helpers.owner_key_vault_fixtures``; expected
values are the plaintexts fed into that factory.
"""

from __future__ import annotations

import dataclasses
import logging
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
    OwnerKeyVersionUnavailableError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_owner_key as _gok,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (
    OwnerKeyInfo,
)
from tests.helpers import DummyCache
from tests.helpers.owner_key_vault_fixtures import VaultWorld, make_vault_world
from tests.helpers.polling_mixin_stub import PollingStub

pytestmark = pytest.mark.asyncio


class _SpotDouble:
    """Spot-side doubles that count every call that would reach Spot.

    Counted as Spot calls: ``async_get_owner_key(force_refresh=True)`` and
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
        # Version served by a plain lookup and by a forced refresh; both default
        # to the world's Spot version. A test sets ``refresh_version`` to model
        # Spot serving the tracker's version after a rotation.
        self.served_version = world.spot_version
        self.refresh_version = world.spot_version
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
        version = self.served_version
        if force_refresh:
            self.force_refresh_calls += 1
            version = self.refresh_version
        return OwnerKeyInfo(key=self._world.owner_keys[version], version=version)

    async def get_eid_info(self, *, cache: object) -> object:
        self.eid_info_calls += 1
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(
                ownerKeyVersion=self.refresh_version
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


async def test_tracker_newer_than_spot_decrypts_from_vault_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry N present -> EIK from the vault entry, no Spot call at all.

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


async def test_tracker_newer_than_spot_without_field_raises_version_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bundle field -> dedicated error with the login remedy.

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


async def test_version_missing_in_field_raises_version_unavailable_with_vault_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field present but entry N missing -> dedicated error, vault remedy.

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
    """N < M keeps ``StaleOwnerKeyError``, even with a vault field."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    _install(monkeypatch, world)
    cache = _seeded_cache(world, world.bundle_field)

    with pytest.raises(StaleOwnerKeyError):
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(1), cache=cache
        )


async def test_version_unavailable_error_never_drives_account_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gap error never advances the reauth counter and never escalates.

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

    with pytest.raises(OwnerKeyVersionUnavailableError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2), cache=cache
        )
    gap_error = ei.value
    assert isinstance(gap_error, DecryptionError)

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


# ---------------------------------------------------------------------------
# Throttle, vault lookup and gap register
# ---------------------------------------------------------------------------

_STRUCTURE_DEFECT_MSG = "Owner key is missing or invalid; please re-authenticate."
_HEX_WINDOW = 16


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Standing monotonic clock for the refresh throttle; tests advance ``[0]``."""
    now = [1_000.0]
    monkeypatch.setattr(_gok, "_monotonic", lambda: now[0])
    return now


class _ScopedCache(_CountingCache):
    """Counting cache with an entry id, like the coordinator's ``TokenCache``."""

    def __init__(self, entry_id: str) -> None:
        super().__init__()
        self.entry_id = entry_id


class _RaisingCache(DummyCache):
    """Cache whose ``get`` raises for names that contain ``fragment``."""

    def __init__(self, fragment: str, message: str = "boom") -> None:
        super().__init__()
        self._fragment = fragment
        self._message = message

    async def get(self, name: str) -> object | None:
        if self._fragment in name:
            raise RuntimeError(self._message)
        return await super().get(name)


async def _expect_gap(
    cache: object, world: VaultWorld
) -> OwnerKeyVersionUnavailableError:
    with pytest.raises(OwnerKeyVersionUnavailableError) as ei:
        await _dl.async_retrieve_identity_key(
            world.tracker_registration(2),
            cache=cache,  # type: ignore[arg-type]
        )
    return ei.value


def _field_without(world: VaultWorld, version: int) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in world.bundle_field.items() if k != str(version)}


async def test_spot_calls_per_cycle(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """Eleven gap reports in one cycle at a standing clock -> one refresh."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, None)

    _gok.begin_owner_key_poll_cycle("entry-1")
    for _ in range(11):
        await _expect_gap(cache, world)

    assert spot.spot_calls == 1
    assert spot.force_refresh_calls == 1


async def test_push_without_cycle_throttled_by_clock(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """Without a poll cycle only the clock counts: 1, then 0, then 1 after 300 s."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _seeded_cache(world, None)

    await _expect_gap(cache, world)
    assert spot.force_refresh_calls == 1
    await _expect_gap(cache, world)
    assert spot.force_refresh_calls == 1

    clock[0] += _gok.OWNER_KEY_SPOT_REFRESH_MIN_INTERVAL_S
    await _expect_gap(cache, world)
    assert spot.force_refresh_calls == 2
    assert spot.eid_info_calls == 0


async def test_vault_key_found_but_eik_fails_keeps_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vault key opens, the EIK does not -> login remedy, no Spot call at all."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)

    async def _refuse_spot(**_kwargs: object) -> object:
        spot.eid_info_calls += 1
        raise RuntimeError("Spot must not be called")

    monkeypatch.setattr(_dl, "async_get_eid_info", _refuse_spot)
    cache = _seeded_cache(world, world.bundle_field)
    shared_before = cache.values["shared_key"]
    registration = world.tracker_registration(2)
    registration.encryptedUserSecrets.encryptedIdentityKey = world.encrypted_eik(1)

    with pytest.raises(OwnerKeyVersionUnavailableError) as ei:
        await _dl.async_retrieve_identity_key(registration, cache=cache)

    assert ei.value.remedy == "login"
    assert ei.value.required_version == 2
    assert ei.value.current_version == 1
    assert ei.value.bundle_versions == (1, 2)
    assert cache.values["shared_key"] == shared_before
    assert cache.key_clears == []
    assert spot.eid_info_calls == 0
    assert spot.force_refresh_calls == 0


async def test_scope_without_entry_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caches without, with empty or blank entry id never raise."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    bare = _seeded_cache(world, None)
    assert not hasattr(bare, "entry_id")
    assert _gok.owner_key_scope(bare) == f"id:{id(bare)}"
    assert _gok.owner_key_scope(SimpleNamespace(entry_id="")).startswith("id:")
    assert _gok.owner_key_scope(SimpleNamespace(entry_id=" ", namespace="ns")) == "ns"
    assert _gok.owner_key_scope(SimpleNamespace(entry_id="e1", namespace="ns")) == "e1"

    error = await _expect_gap(bare, world)
    assert error.remedy == "login"


async def test_gap_resolved_after_spot_serves_required_version(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """Cycle 1 fails (vault lacks N), cycle 2 Spot serves N -> resolved via 1094."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, _field_without(world, 2))

    _gok.begin_owner_key_poll_cycle("entry-1")
    error = await _expect_gap(cache, world)
    assert error.remedy == "vault"
    assert _gok.consume_resolved_owner_key_versions("entry-1") == ()

    spot.refresh_version = 2
    _gok.begin_owner_key_poll_cycle("entry-1")
    eiks = await _dl.async_retrieve_identity_key(
        world.tracker_registration(2), cache=cache
    )

    assert world.eiks[2] in eiks
    assert spot.eid_info_calls == 1  # the shared-key source failed: metadata path
    assert _gok.consume_resolved_owner_key_versions("entry-1") == (2,)


async def test_gap_resolved_on_clean_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spot serves N and every EIK source opens -> resolved at the clean return."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    spot.refresh_version = 2
    monkeypatch.setattr(_dl, "decrypt_eik", lambda _key, _blob: world.eiks[2])
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, None)

    eiks = await _dl.async_retrieve_identity_key(
        world.tracker_registration(2), cache=cache
    )

    assert eiks == [world.eiks[2]]
    assert spot.eid_info_calls == 0
    assert _gok.consume_resolved_owner_key_versions("entry-1") == (2,)


async def test_vault_hit_records_resolved_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The early vault return records N as resolved and leaves no open gap."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    _install(monkeypatch, world)
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, world.bundle_field)

    await _dl.async_retrieve_identity_key(world.tracker_registration(2), cache=cache)

    assert _gok.open_owner_key_version_gaps("entry-1") == ()
    assert _gok.consume_resolved_owner_key_versions("entry-1") == (2,)
    assert _gok.consume_resolved_owner_key_versions("entry-1") == ()


def _never_raises_case(
    case: str, world: VaultWorld, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, str]:
    if case == "object_cache":
        return object(), "field_missing"
    if case == "get_raises":
        return _RaisingCache("owner_key_versions"), "field_missing"
    if case == "shared_key_read_raises":
        cache = _RaisingCache("shared_key")
        cache.values["owner_key_versions"] = world.bundle_field
        return cache, "field_missing"
    if case == "foreign_field":
        cache = DummyCache()
        world.seed_entry_cache(cache.values, {"2": {"version": "two"}})
        return cache, "undecryptable"
    if case == "invalid_tag":
        cache = DummyCache()
        other = make_vault_world(versions=(1, 2), spot_version=1)
        other.seed_entry_cache(cache.values, None)
        cache.values["owner_key_versions"] = world.bundle_field
        return cache, "undecryptable"
    assert case == "short_result"
    cache = DummyCache()
    world.seed_entry_cache(cache.values, world.bundle_field)
    monkeypatch.setattr(_gok, "decrypt_owner_key", lambda _k, _b: b"\x01" * 31)
    return cache, "undecryptable"


@pytest.mark.parametrize(
    "case",
    [
        "object_cache",
        "get_raises",
        "shared_key_read_raises",
        "foreign_field",
        "invalid_tag",
        "short_result",
    ],
)
async def test_vault_lookup_never_raises(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every failure maps to a status, ``key`` stays ``None``, nothing raises."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    cache, expected = _never_raises_case(case, world, monkeypatch)

    lookup = await _gok.async_get_vault_owner_key(
        cache=cache,  # type: ignore[arg-type]
        version=2,
    )

    assert lookup.status == expected
    assert lookup.key is None


async def test_vault_lookup_logs_only_class_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The DEBUG line names the exception class, never its text."""
    cache = _RaisingCache("owner_key_versions", message="SENTINEL-7f3a")
    with caplog.at_level(logging.DEBUG, logger=_gok.__name__):
        lookup = await _gok.async_get_vault_owner_key(
            cache=cache,  # type: ignore[arg-type]
            version=2,
        )

    assert lookup.status == "field_missing"
    assert "SENTINEL-7f3a" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_vault_lookup_repr_hides_key() -> None:
    """Repr and str of a found lookup show no key bytes; it is frozen."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    cache = DummyCache()
    world.seed_entry_cache(cache.values, world.bundle_field)

    lookup = await _gok.async_get_vault_owner_key(
        cache=cache,  # type: ignore[arg-type]
        version=2,
    )

    assert lookup.status == "found"
    assert lookup.key == world.owner_keys[2]
    key_hex = world.owner_keys[2].hex()
    windows = {
        key_hex[i : i + _HEX_WINDOW] for i in range(len(key_hex) - _HEX_WINDOW + 1)
    }
    for text in (repr(lookup), str(lookup), "%s" % (lookup,)):  # noqa: UP031
        assert "b'" not in text
        assert 'b"' not in text
        assert "\\x" not in text
        assert not any(window in text.lower() for window in windows)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lookup.status = "field_missing"  # type: ignore[misc]


async def test_reset_owner_key_refresh_state_clears_register(
    clock: list[float],
) -> None:
    """The reset seam empties the register, so a denied claim is granted again."""
    cache = SimpleNamespace(entry_id="x")
    assert _gok.claim_owner_key_spot_refresh(cache) is True
    assert _gok.claim_owner_key_spot_refresh(cache) is False
    _gok.reset_owner_key_refresh_state()
    assert _gok.claim_owner_key_spot_refresh(cache) is True


async def test_refresh_state_reset_fixture_is_autouse(
    request: pytest.FixtureRequest,
) -> None:
    """The conftest fixture isolates the register for every test."""
    assert "isolate_owner_key_refresh_state" in request.fixturenames


async def test_resolver_clock_claim_keeps_poll_claim(clock: list[float]) -> None:
    """The resolver's clock claim never consumes the poll-cycle claim."""
    cycle_cache = SimpleNamespace(entry_id="e")
    _gok.begin_owner_key_poll_cycle("e")
    assert _gok.claim_owner_key_spot_refresh_by_clock(cycle_cache) is True
    assert _gok.claim_owner_key_spot_refresh(cycle_cache) is True

    push_cache = SimpleNamespace(entry_id="p")
    assert _gok.claim_owner_key_spot_refresh_by_clock(push_cache) is True
    assert _gok.claim_owner_key_spot_refresh(push_cache) is True
    assert _gok.claim_owner_key_spot_refresh(push_cache) is False

    reverse_cache = SimpleNamespace(entry_id="r")
    assert _gok.claim_owner_key_spot_refresh(reverse_cache) is True
    assert _gok.claim_owner_key_spot_refresh_by_clock(reverse_cache) is True
    assert _gok.claim_owner_key_spot_refresh_by_clock(reverse_cache) is False


async def test_rederive_success_records_open_gap_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structural-defect re-derive that yields N records the open gap of N."""
    world = make_vault_world(versions=(1, 2), spot_version=2)
    spot = _install(monkeypatch, world)
    calls = {"refresh": 0}

    async def _rederive_fake(
        *,
        cache: object,
        username: str | None = None,
        eid_info_getter: Callable[[], Awaitable[Any]] | None = None,
        shared_key_getter: Callable[[], Awaitable[bytes]] | None = None,
        force_refresh: bool = False,
    ) -> OwnerKeyInfo:
        if not force_refresh:
            raise RuntimeError(_STRUCTURE_DEFECT_MSG)
        calls["refresh"] += 1
        return OwnerKeyInfo(key=world.owner_keys[2], version=2)

    monkeypatch.setattr(_dl, "async_get_owner_key", _rederive_fake)
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, None)
    _gok.record_owner_key_version_gap(cache, 2)

    eiks = await _dl.async_retrieve_identity_key(
        world.tracker_registration(2), cache=cache
    )

    assert world.eiks[2] in eiks
    assert calls["refresh"] == 1
    assert spot.eid_info_calls == 1
    assert _gok.consume_resolved_owner_key_versions("entry-1") == (2,)
    assert _gok.open_owner_key_version_gaps("entry-1") == ()


async def test_record_without_open_gap_is_noop() -> None:
    """Recording a version that never opened a gap changes nothing.

    The scope exists (version 3 is open), so the membership check is what keeps
    version 5 out, not a missing register entry.
    """
    cache = SimpleNamespace(entry_id="entry-1")
    _gok.record_owner_key_version_gap(cache, 3)
    _gok.record_owner_key_version_resolved(cache, 5)
    _gok.record_owner_key_version_resolved(cache, None)
    _gok.record_owner_key_version_resolved(cache, 0)
    assert _gok.consume_resolved_owner_key_versions("entry-1") == ()
    assert _gok.open_owner_key_version_gaps("entry-1") == (3,)


async def test_gap_entry_opens_version_until_success(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """The raised gap stays open in its scope until a later success heals it."""
    world = make_vault_world(versions=(1, 2), spot_version=1)
    spot = _install(monkeypatch, world)
    cache = _ScopedCache("entry-1")
    world.seed_entry_cache(cache.values, _field_without(world, 2))

    _gok.begin_owner_key_poll_cycle("entry-1")
    await _expect_gap(cache, world)
    assert _gok.open_owner_key_version_gaps("entry-1") == (2,)

    spot.refresh_version = 2
    _gok.begin_owner_key_poll_cycle("entry-1")
    await _dl.async_retrieve_identity_key(world.tracker_registration(2), cache=cache)
    assert _gok.open_owner_key_version_gaps("entry-1") == ()

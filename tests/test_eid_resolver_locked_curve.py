# tests/test_eid_resolver_locked_curve.py
"""The resolver tells the decryption path which curve a device is locked to.

A foreign report with a 20-byte ``Sx`` cannot authenticate for a device whose
EID lies on P-256, so the decryption path counts it as key-neutral instead of
as a stale-key signal. It learns the curve from
``GoogleFindMyEIDResolver.locked_curve_name`` through
``FOREIGN_READING_TRACKER``; these tests pin that lookup and its registration.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.eid_resolver import (
    EIDGenerationLock,
    EidVariant,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.curve_profile import (
    SECP160R1,
    SECP256R1,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.foreign_reading_tracker import (
    FOREIGN_READING_TRACKER,
)


def _close_coro(coro: object, name: object = None) -> None:
    close = getattr(coro, "close", None)
    if close is not None:
        close()


def _lock(device_id: str, canonical_id: str, variant: str) -> EIDGenerationLock:
    return EIDGenerationLock(
        device_id=device_id,
        canonical_id=canonical_id,
        variant=variant,
        advertisement_reversed=False,
        eid_length=20,
        rotation_timestamp=1_700_000_000,
    )


def _resolver(*locks: EIDGenerationLock) -> GoogleFindMyEIDResolver:
    """Return a resolver whose only state is ``locks`` (stop() can run on it)."""
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=_close_coro, async_create_background_task=_close_coro
    )
    resolver._locks = {lock.device_id: lock for lock in locks}
    resolver._persisted_locks = dict(resolver._locks)
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._provisioning_warn_at = {}
    resolver._truncated_frame_log_at = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._load_task = None
    return resolver


_VARIANT_CURVES: list[tuple[EidVariant, str]] = [
    (EidVariant.MODERN_P256_X20_TRUNC_BE, SECP256R1.name),
    (EidVariant.MODERN_P256_X20_TRUNC_LE, SECP256R1.name),
    (EidVariant.MODERN_P256_X32_BE, SECP256R1.name),
    (EidVariant.MODERN_P256_X32_LE_SCALAR, SECP256R1.name),
    (EidVariant.SPEC_P256_X32_BE, SECP256R1.name),
    (EidVariant.SPEC_P256_X20_TRUNC_BE, SECP256R1.name),
    (EidVariant.LEGACY_SECP160R1_X20_BE, SECP160R1.name),
]


@pytest.mark.parametrize(("variant", "curve"), _VARIANT_CURVES)
def test_every_variant_maps_to_its_curve(variant: EidVariant, curve: str) -> None:
    resolver = _resolver(_lock("dev-1", "abc123", variant.value))

    assert resolver.locked_curve_name("abc123") == curve


def test_every_eid_variant_is_covered() -> None:
    """A new variant must be added to the parametrization above."""
    assert {variant for variant, _curve in _VARIANT_CURVES} == set(EidVariant)
    resolver = _resolver()
    for variant in EidVariant:
        resolver._locks = {"dev-1": _lock("dev-1", "abc123", variant.value)}
        assert resolver.locked_curve_name("abc123") is not None, variant


def test_a_variant_without_a_curve_row_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent fallback: the tracker turns the error into ``None``."""
    monkeypatch.delitem(
        eid_resolver._VARIANT_CURVE_PARAMS, EidVariant.MODERN_P256_X20_TRUNC_BE
    )
    resolver = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value)
    )

    with pytest.raises(KeyError):
        resolver.locked_curve_name("abc123")

    FOREIGN_READING_TRACKER.set_curve_provider(resolver.locked_curve_name)
    assert FOREIGN_READING_TRACKER.locked_curve("abc123") is None


def test_casing_and_namespace_do_not_matter() -> None:
    """A stored lock may keep the namespaced form until the next refresh."""
    resolver = _resolver(
        _lock("dev-1", "account:ABC123", EidVariant.MODERN_P256_X20_TRUNC_BE.value)
    )

    assert resolver.locked_curve_name("abc123") == SECP256R1.name
    assert resolver.locked_curve_name("other:AbC123") == SECP256R1.name


def test_no_curve_without_a_lock_of_that_device() -> None:
    resolver = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value)
    )

    assert resolver.locked_curve_name("def456") is None


def test_no_curve_for_an_unknown_variant() -> None:
    resolver = _resolver(_lock("dev-1", "abc123", "not_a_variant"))

    assert resolver.locked_curve_name("abc123") is None


def test_no_curve_when_two_locks_of_one_device_disagree() -> None:
    resolver = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value),
        _lock("dev-2", "ABC123", EidVariant.LEGACY_SECP160R1_X20_BE.value),
    )

    assert resolver.locked_curve_name("abc123") is None


def test_two_agreeing_locks_give_their_curve() -> None:
    resolver = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value),
        _lock("dev-2", "abc123", EidVariant.MODERN_P256_X32_BE.value),
    )

    assert resolver.locked_curve_name("abc123") == SECP256R1.name


def test_post_init_registers_and_stop_unregisters_the_lookup() -> None:
    resolver = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value)
    )
    with (
        patch("custom_components.googlefindmy.eid_resolver.Store", MagicMock()),
        patch.object(GoogleFindMyEIDResolver, "_start_alignment_timer"),
    ):
        resolver.__post_init__()

    assert FOREIGN_READING_TRACKER.locked_curve("abc123") == SECP256R1.name

    resolver.stop()

    assert FOREIGN_READING_TRACKER._curve_provider is None
    assert FOREIGN_READING_TRACKER.locked_curve("abc123") is None


def test_stop_of_an_older_resolver_keeps_the_newer_lookup() -> None:
    older = _resolver()
    newer = _resolver(
        _lock("dev-1", "abc123", EidVariant.MODERN_P256_X20_TRUNC_BE.value)
    )
    FOREIGN_READING_TRACKER.set_curve_provider(older.locked_curve_name)
    FOREIGN_READING_TRACKER.set_curve_provider(newer.locked_curve_name)

    older.stop()

    assert FOREIGN_READING_TRACKER.locked_curve("abc123") == SECP256R1.name

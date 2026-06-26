# tests/test_eid_resolver_optimization.py
"""Optimization coverage for known time-basis pruning."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    EidVariant,
)


async def _run_in_executor(func, *args):
    """Synchronous stand-in for ``hass.async_add_executor_job`` (AP-C)."""

    return func(*args)


@pytest.mark.asyncio
async def test_known_time_basis_skips_alternate_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the time basis is known, skip alternate counter strategies."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=asyncio.create_task,
        async_add_executor_job=_run_in_executor,
        data={},
    )
    resolver._store = SimpleNamespace(
        async_load=AsyncMock(return_value=None),
        async_save=AsyncMock(),
    )
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._provisioning_warn_at = {}
    resolver._locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    now_unix = 1000
    pair_target = 100
    secrets_target = 200

    resolver._locks["tracker-1"] = EIDGenerationLock(
        device_id="tracker-1",
        canonical_id="can-1",
        variant=EidVariant.LEGACY_SECP160R1_X20_BE.value,
        advertisement_reversed=False,
        eid_length=LEGACY_EID_LENGTH,
        rotation_timestamp=None,
        time_basis="pair_date",
    )

    identity = DeviceIdentity(
        registry_id="tracker-1",
        canonical_id="can-1",
        identity_key=b"\x01" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-1",
        fast_pair_model_id=None,
        pair_date=now_unix - pair_target,
        secrets_creation_date=now_unix - secrets_target,
    )

    monkeypatch.setattr(time, "time", lambda: float(now_unix))
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        AsyncMock(return_value=[identity]),
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )

    seen_windows: list[int] = []

    def _iter_rotation_windows(
        target_time: int,
        *,
        rotation_period: int,
        window_range,
        include_neighbors: bool,
    ) -> tuple[int, ...]:
        _ = (rotation_period, window_range, include_neighbors)
        seen_windows.append(target_time)
        return (target_time + 1,)

    monkeypatch.setattr(
        resolver_module, "iter_rotation_windows", _iter_rotation_windows
    )

    seen_counters: list[int] = []

    def _generate_variant(
        _self: GoogleFindMyEIDResolver,
        _key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        _ = variant
        seen_counters.append(time_counter)
        return b"\x11" * LEGACY_EID_LENGTH

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _generate_variant)

    await resolver._refresh_cache()

    assert resolver._known_timebases["tracker-1"] == "pair_date"
    # The known basis hint narrows the relative-window scan to the single
    # ``pair_date`` basis: every window/counter explored equals the pair target,
    # and no secrets/unix basis is tried. Under the AP-B1 skip guard (Variant B)
    # the pure window math runs once for the build signature and once for the
    # build itself, so each list repeats the single value rather than appearing
    # once; the expensive crypto stays single-pass via the AP-A memo. What this
    # test pins is the *basis narrowing*, i.e. uniqueness, not the call count.
    assert set(seen_windows) == {pair_target}
    assert set(seen_counters) == {pair_target + 1}

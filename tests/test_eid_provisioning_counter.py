"""Provisioning counter and timestamp normalization tests for the EID resolver."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    ROTATION_PERIOD,
    generate_eid,
)


@pytest.mark.asyncio
async def test_resolver_matches_counter_timebase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provisioning counters should drive EID matching instead of Unix time."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(data={})
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._unsub_alignment = None
    resolver._unsub_interval = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    identity_key = bytes.fromhex("00" * 32)
    pair_date = 1_700_000_000
    now_unix = pair_date + (ROTATION_PERIOD * 10)
    counter = (now_unix - pair_date) & 0xFFFFFFFF
    base_counter = counter & ~(ROTATION_PERIOD - 1)

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=identity_key,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=pair_date,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _collect,
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: float(now_unix))
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.dr.async_get",
        lambda hass: None,
    )

    eid = generate_eid(identity_key, base_counter)
    payload = bytes([FMDN_FRAME_TYPE]) + eid + b"\x00"

    await resolver._refresh_cache()

    match = resolver.resolve_eid(payload)

    assert match is not None
    assert match.device_id == "registry-id"
    assert match.time_offset == 0
    metadata = resolver._lookup_metadata.get(eid)
    assert metadata is not None
    assert metadata.get("timestamp_basis") == "counter:pair_date"
    assert metadata.get("anchor_epoch") == pair_date

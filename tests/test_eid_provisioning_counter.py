# tests/test_eid_provisioning_counter.py
"""Timestamp normalization tests for the EID resolver."""

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
async def test_resolver_matches_unix_timebase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver should match EIDs generated from the current unix rotation window."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(data={}, async_create_task=lambda coro: asyncio.create_task(coro))
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    async def _async_noop(payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_alignment = None
    resolver._unsub_interval = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity_key = bytes.fromhex("00" * 32)
    now_unix = 1_700_000_000
    base_counter = (now_unix - (now_unix % ROTATION_PERIOD))

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=identity_key,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
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

    eid = generate_eid(identity_key, base_counter)
    payload = bytes([FMDN_FRAME_TYPE]) + eid + b"\x00"

    await resolver._refresh_cache()

    match = resolver.resolve_eid(payload)

    assert match is not None
    assert match.device_id == "registry-id"
    assert abs(match.time_offset) < ROTATION_PERIOD

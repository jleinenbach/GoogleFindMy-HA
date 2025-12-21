# tests/test_eid_resolver.py
"""Resolver behavior for FHNA/FMDN EIDs."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EIDMatch,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
)


def _fake_hass() -> SimpleNamespace:
    """Return a lightweight hass stand-in."""

    return SimpleNamespace(
        async_create_task=lambda coro: asyncio.create_task(coro),
        data={},
    )


@pytest.mark.asyncio
async def test_resolver_extracts_service_data_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver should parse a service-data payload with frame type at octet 7."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    async def _async_noop(payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\xAA" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    await resolver._refresh_cache()

    eid = next(iter(resolver._lookup))
    service_data = b"\x00" * 7 + bytes([resolver_module.FMDN_FRAME_TYPE]) + eid + b"\x99"

    match = resolver.resolve_eid(service_data)
    assert match is not None
    assert isinstance(match, EIDMatch)
    assert match.device_id == "registry-id"
    assert resolver._locks[match.device_id].frame_type == resolver_module.FMDN_FRAME_TYPE


@pytest.mark.asyncio
async def test_resolver_saves_locks_via_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks should persist through the store helper."""

    saved_payload: list[dict] | None = None

    async def _async_save(payload: list[dict] | None) -> None:
        nonlocal saved_payload
        saved_payload = payload

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_save)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity = DeviceIdentity(
        registry_id="persist-id",
        canonical_id="canonical-id",
        identity_key=b"\xBB" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: 2048.0)

    await resolver._refresh_cache()
    eid = next(iter(resolver._lookup))
    payload = bytes([resolver_module.FMDN_FRAME_TYPE]) + eid

    match = resolver.resolve_eid(payload)
    assert match is not None

    await asyncio.sleep(0)  # allow async_save scheduling
    assert saved_payload is not None
    assert saved_payload[0]["device_id"] == "persist-id"


@pytest.mark.asyncio
async def test_resolver_handles_raw_eid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw 20-byte payloads should match without frame metadata."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    async def _async_noop(payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity = DeviceIdentity(
        registry_id="raw-id",
        canonical_id="raw-canonical",
        identity_key=b"\xCC" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: 4096.0)

    await resolver._refresh_cache()

    eid = next(iter(resolver._lookup))
    match = resolver.resolve_eid(eid)

    assert match is not None
    assert match.device_id == "raw-id"
    assert resolver._locks[match.device_id].eid_length == LEGACY_EID_LENGTH


def test_extract_candidates_sliding_window() -> None:
    """Resolver should fall back to a bounded sliding window for unexpected payloads."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    payload = b"\x00" * 10 + b"A" * LEGACY_EID_LENGTH + b"\xFF" * 10

    candidates, frame = resolver._extract_candidates(payload)
    assert frame is None
    assert payload[10:10 + LEGACY_EID_LENGTH] in candidates

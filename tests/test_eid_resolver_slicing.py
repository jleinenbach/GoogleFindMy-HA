# tests/test_eid_resolver_slicing.py
"""Tests for normalizing raw BLE payloads before EID resolution."""

import asyncio
from types import SimpleNamespace

from custom_components.googlefindmy.eid_resolver import (
    EIDMatch,
    GoogleFindMyEIDResolver,
)


def _build_resolver(lookup_key: bytes, match: EIDMatch) -> GoogleFindMyEIDResolver:
    """Construct a resolver instance with a single lookup entry."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace()
    resolver._lookup = {lookup_key: match}
    resolver._lookup_metadata = {
        lookup_key: {
            "timestamp_basis": "pair_date",
            "variant": match.time_offset,
        }
    }
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._provisioning_warn_at = {}
    return resolver


def test_resolve_eid_slices_raw_payload() -> None:
    """Raw service data should skip the header and slice the EID portion."""

    eid = bytes.fromhex("0102030405060708090a0b0c0d0e0f1011121314")
    match = EIDMatch(
        device_id="device-raw",
        config_entry_id="entry-raw",
        canonical_id="canonical-raw",
        time_offset=0,
        is_reversed=False,
    )
    resolver = _build_resolver(eid, match)
    payload = b"\x40" + eid + b"\x30"

    resolved = resolver.resolve_eid(payload)

    assert resolved == match
    assert resolver._known_offsets.get(("device-raw", "pair_date")) == 0
    assert resolver._known_advertisement_reversed.get("device-raw") is False


def test_resolve_eid_accepts_pure_eid() -> None:
    """Exact-length payloads should resolve without slicing."""

    eid = bytes.fromhex("ffffffffffffffffffffffffffffffffffffffff")
    match = EIDMatch(
        device_id="device-pure",
        config_entry_id="entry-pure",
        canonical_id="canonical-pure",
        time_offset=5,
        is_reversed=True,
    )
    resolver = _build_resolver(eid, match)

    resolved = resolver.resolve_eid(eid)

    assert resolved == match
    assert resolver._known_offsets.get(("device-pure", "pair_date")) == 5
    assert resolver._known_advertisement_reversed.get("device-pure") is True


def test_resolve_eid_rejects_short_payload() -> None:
    """Payloads shorter than the 20-byte EID should return ``None``."""

    resolver = _build_resolver(
        b"\x00" * 20,
        EIDMatch(
            device_id="device-short",
            config_entry_id="entry-short",
            canonical_id="canonical-short",
            time_offset=3,
            is_reversed=False,
        ),
    )

    assert resolver.resolve_eid(b"\x01" * 19) is None
    assert resolver._known_offsets == {}
    assert resolver._known_advertisement_reversed == {}

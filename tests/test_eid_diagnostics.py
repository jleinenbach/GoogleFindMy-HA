import asyncio
import logging

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    EIDMatch,
    GoogleFindMyEIDResolver,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import EidCandidate


def _build_resolver() -> GoogleFindMyEIDResolver:
    class _Resolver(resolver_module.GoogleFindMyEIDResolver):
        def _start_alignment_timer(self) -> None:
            return None

    instance = _Resolver(hass=object())
    instance._lookup = {}
    instance._known_offsets = {}
    instance._known_endianness = {}
    instance._unsub_interval = None
    instance._unsub_alignment = None
    instance._refresh_lock = asyncio.Lock()
    instance._pending_refresh = False
    return instance


@pytest.mark.asyncio
async def test_refresh_cache_logs_debug_dump(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    identity = DeviceIdentity(
        registry_id="registry-dump",
        canonical_id="device-d329",
        identity_key=b"\x01" * 32,
        config_entry_id="entry-dump",
    )
    resolver = _build_resolver()

    base_time = 4096
    monkeypatch.setattr(resolver_module.time, "time", lambda: base_time)

    def _fake_candidates(key: bytes, timestamp: int) -> tuple[EidCandidate, ...]:
        return (
            EidCandidate(
                name="legacy", eid=(key + timestamp.to_bytes(8, "big"))[:EID_LENGTH]
            ),
        )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(resolver_module, "_cached_candidates", _fake_candidates)
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

    caplog.set_level(logging.DEBUG)

    await resolver._refresh_cache()

    assert any(
        record.levelno == logging.DEBUG
        and "DEBUG DUMP: Device=device-d329" in record.message
        and "Variant=legacy" in record.message
        for record in caplog.records
    )


def test_resolve_eid_logs_match_found(caplog: pytest.LogCaptureFixture) -> None:
    resolver = _build_resolver()
    lookup_key = b"\x02" * EID_LENGTH
    match = EIDMatch(
        device_id="device-logs",
        config_entry_id="entry-logs",
        canonical_id="canonical-logs",
        time_offset=0,
        is_reversed=False,
    )
    resolver._lookup = {lookup_key: match}

    caplog.set_level(logging.INFO)

    assert resolver.resolve_eid(lookup_key) is match
    assert any(
        record.levelno == logging.INFO
        and "HIT: device=device-logs" in record.message
        for record in caplog.records
    )


def test_reset_device_offset_clears_cache() -> None:
    resolver = _build_resolver()
    resolver._known_offsets = {"device-clear": 10}
    resolver._known_endianness = {"device-clear": True}

    resolver.reset_device_offset("device-clear")

    assert resolver._known_offsets == {}
    assert resolver._known_endianness == {}

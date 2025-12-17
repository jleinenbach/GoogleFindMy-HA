from types import SimpleNamespace

from custom_components.googlefindmy.eid_resolver import (
    EIDMatch,
    GoogleFindMyEIDResolver,
)


def _build_resolver(lookup_key: bytes, match: EIDMatch) -> GoogleFindMyEIDResolver:
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace()
    resolver._lookup = {lookup_key: match}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    return resolver


def test_fmdn_frame_required_for_extended_payload() -> None:
    eid = bytes.fromhex("00112233445566778899aabbccddeeff00112233")
    match = EIDMatch(
        device_id="device-fmdn",
        config_entry_id="entry-fmdn",
        canonical_id="canonical-fmdn",
        time_offset=0,
        is_reversed=False,
    )
    resolver = _build_resolver(eid, match)

    payload = b"\x40" + eid + b"\x00\x01"
    resolved = resolver.resolve_eid(payload)

    assert resolved == match

    non_fmdn_payload = b"\x41" + eid + b"\x00\x01"
    assert resolver.resolve_eid(non_fmdn_payload) is None


def test_exact_length_payloads_bypass_frame_filter() -> None:
    eid = bytes.fromhex("abcdefabcdefabcdefabcdefabcdefabcdefabcd")
    match = EIDMatch(
        device_id="device-legacy",
        config_entry_id="entry-legacy",
        canonical_id="canonical-legacy",
        time_offset=4,
        is_reversed=True,
    )
    resolver = _build_resolver(eid, match)

    assert resolver.resolve_eid(eid) == match

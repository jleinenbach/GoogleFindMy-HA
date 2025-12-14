import logging

import pytest

from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    RAW_HEADER_LENGTH,
    EIDMatch,
    GoogleFindMyEIDResolver,
)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    monkeypatch.setattr(
        GoogleFindMyEIDResolver, "_start_alignment_timer", lambda self: None
    )
    return GoogleFindMyEIDResolver(hass=object())


def _probe_message(records: list[logging.LogRecord]) -> list[str]:
    return [record.message for record in records if record.levelno == logging.DEBUG]


def test_match_triggers_info_log(
    resolver: GoogleFindMyEIDResolver, caplog: pytest.LogCaptureFixture
) -> None:
    lookup_key = b"\x01" * EID_LENGTH
    match = EIDMatch(
        device_id="device-1",
        config_entry_id="entry-1",
        canonical_id="canonical-1",
        time_offset=0,
        is_reversed=False,
    )
    resolver._lookup = {lookup_key: match}

    caplog.set_level(logging.DEBUG)

    assert resolver.resolve_eid(lookup_key) is match
    assert any(
        "HIT: device=" in record.message and record.levelno == logging.INFO
        for record in caplog.records
    )


def test_non_match_does_not_log_info(
    resolver: GoogleFindMyEIDResolver, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    assert resolver.resolve_eid(b"\x02" * EID_LENGTH) is None
    assert not any(record.levelno == logging.INFO for record in caplog.records)


def test_probe_logs_sliced_key(
    resolver: GoogleFindMyEIDResolver, caplog: pytest.LogCaptureFixture
) -> None:
    raw_payload = b"\x40" + (b"\x03" * EID_LENGTH) + b"\xFF"
    expected_lookup = raw_payload[RAW_HEADER_LENGTH : RAW_HEADER_LENGTH + EID_LENGTH]
    resolver._lookup = {}

    caplog.set_level(logging.DEBUG)

    resolver.resolve_eid(raw_payload)

    probe_messages = _probe_message(caplog.records)
    assert any(expected_lookup[:4].hex() in message for message in probe_messages)

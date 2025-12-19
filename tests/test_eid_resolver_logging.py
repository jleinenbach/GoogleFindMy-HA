# tests/test_eid_resolver_logging.py
import asyncio
import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    EIDMatch,
    GoogleFindMyEIDResolver,
)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    async def _async_noop(payload=None):
        return None

    hass = SimpleNamespace(
        async_create_task=lambda coro: None,
        data={},
    )

    instance = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    instance.hass = hass
    instance._lookup = {}
    instance._lookup_metadata = {}
    instance._locks = {}
    instance._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    instance._unsub_interval = None
    instance._unsub_alignment = None
    instance._refresh_lock = asyncio.Lock()
    instance._pending_refresh = False
    instance._load_task = None
    return instance


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
    resolver._lookup = {}

    caplog.set_level(logging.DEBUG)

    resolver.resolve_eid(raw_payload)

    probe_messages = _probe_message(caplog.records)
    assert any("prefix=" in message for message in probe_messages)

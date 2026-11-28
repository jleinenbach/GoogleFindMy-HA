# tests/test_decoder_formatter_time.py
"""custom_message_formatter renders a singular ``Time`` field (issue #1492)."""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder


def _message() -> DeviceUpdate_pb2.RecentLocationAndNetworkLocations:
    msg = DeviceUpdate_pb2.RecentLocationAndNetworkLocations()
    msg.recentLocationTimestamp.seconds = 1_800_000_000
    msg.recentLocationTimestamp.nanos = 500_000_000
    return msg


def test_single_time_field_renders_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLEFINDMY_DEV_TZ", raising=False)
    text = decoder.custom_message_formatter(_message(), "", False)
    assert text == "recentLocationTimestamp {\n  utc: 2027-01-15T08:00:00.500000Z\n}"


def test_single_time_field_adds_display_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLEFINDMY_DEV_TZ", "Europe/Berlin")
    text = decoder.custom_message_formatter(_message(), "", False)
    assert text == (
        "recentLocationTimestamp {\n  utc: 2027-01-15T08:00:00.500000Z\n"
        "  Europe/Berlin: 2027-01-15T09:00:00.500000+01:00\n}"
    )

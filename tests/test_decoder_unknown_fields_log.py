# tests/test_decoder_unknown_fields_log.py
"""The unknown-field diagnostic names field numbers, never field values.

`get_devices_with_location` reports schema drift (a device message carrying
fields the vendored `.proto` does not know) at DEBUG. The record used to
quote the text-format lines of those fields, which carry the server-supplied
values (`AGENTS.md` section 5: raw API payloads never reach the log); it now
carries the field numbers and their count. On current protobuf runtimes
`str(message)` omits unknown fields, so the old probe was silent as well as
unsafe: the helper reads the text format with unknown fields printed.
"""

from __future__ import annotations

import logging

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    decoder._reset_canonicless_warning_state()


def _device_with_unknown_fields(secret: str) -> DeviceUpdate_pb2.DevicesList:
    device_list = DeviceUpdate_pb2.DevicesList()
    device = device_list.deviceMetadata.add()
    device.userDefinedDeviceName = "Pixel"
    # Re-parse the device with two fields the schema does not know: 1023
    # (length-delimited, the secret) and 1024 (varint). Tag 1023<<3|2 is
    # b"\xfa\x3f", tag 1024<<3|0 is b"\x80\x40".
    raw = (
        device.SerializeToString()
        + b"\xfa\x3f"
        + bytes([len(secret)])
        + secret.encode()
        + b"\x80\x40\x07"
    )
    device.ParseFromString(raw)
    return device_list


def test_unknown_field_numbers_reads_numbers_not_values() -> None:
    device_list = _device_with_unknown_fields("s3cr3t-value")
    numbers = decoder._unknown_field_numbers(device_list.deviceMetadata[0])
    assert numbers == [1023, 1024]
    assert decoder._unknown_field_numbers(DeviceUpdate_pb2.DeviceMetadata()) == []


def test_unknown_fields_record_omits_values(caplog: pytest.LogCaptureFixture) -> None:
    secret = "ya29-like-value-7VzWc8ghUGrOpN1J"
    device_list = _device_with_unknown_fields(secret)

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=None)

    records = [
        r.getMessage() for r in caplog.records if "UNKNOWN FIELDS" in r.getMessage()
    ]
    assert len(records) == 1, caplog.text
    assert "numbers=[1023, 1024], count=2" in records[0]
    assert secret not in caplog.text
    assert all(secret[i : i + 8] not in caplog.text for i in range(len(secret) - 7))


def test_unknown_field_numbers_do_not_serialise_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe reads the unknown-field set, never the text format.

    It runs on the event loop inside the DEBUG branch, twice per poll; a
    ``text_format`` pass over every device message would stall the loop, and
    so would a lazy ``import_module`` on first use (``AGENTS.md`` 11.3).
    Nested messages are walked as well.
    """

    def _no_text_format() -> None:
        raise AssertionError("text_format must not be used for unknown fields")

    def _no_import(_name: str) -> None:
        raise AssertionError("no lazy import on the event-loop path")

    monkeypatch.setattr(decoder, "_get_text_format", _no_text_format)
    monkeypatch.setattr(decoder, "import_module", _no_import)
    device_list = _device_with_unknown_fields("s3cr3t-value")
    assert decoder._unknown_field_numbers(device_list.deviceMetadata[0]) == [1023, 1024]
    assert decoder._unknown_field_numbers(device_list) == [1023, 1024]

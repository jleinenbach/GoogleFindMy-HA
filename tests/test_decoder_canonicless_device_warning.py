"""Observability for devices that Google returns without a canonical ID.

When Google's device list contains a device but provides no canonical ID for it
(empty ``canonicIds`` list, or only blank/invalid IDs), ``get_devices_with_location``
emits zero rows for that device, so it silently disappears from Home Assistant. The
user has no way to tell why a phone is missing.

These tests pin a single, de-duplicated WARNING per affected device that names the
device and tells the user how to recover, while guaranteeing that valid devices stay
silent (no behavioural change to the happy path).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder


@pytest.fixture(autouse=True)
def _reset_canonicless_warning_state() -> None:
    """Clear the process-wide warned-devices guard before each test.

    The decoder de-duplicates the canonicless WARNING via a module-level set. Without
    a reset the warning would be suppressed across tests depending on run order, so the
    guard is cleared up front to keep every test order-independent.
    """
    decoder._reset_canonicless_warning_state()


def _make_phone_device(
    *,
    name: str,
    canonic_ids: list[SimpleNamespace],
) -> SimpleNamespace:
    """Build a minimal Android phone device with a configurable canonic-ID list."""
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=DeviceUpdate_pb2.IDENTIFIER_ANDROID,
            phoneInformation=SimpleNamespace(
                canonicIds=SimpleNamespace(canonicId=canonic_ids)
            ),
        ),
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        HasField=lambda field_name: field_name
        not in ("information", "deviceRegistration"),
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
            (SimpleNamespace(name="imageInformation"), None),
        ],
    )


def _canonicless_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return WARNING messages that announce a canonicless device drop."""
    return [
        rec.message
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "canonical ID" in rec.message
    ]


def test_empty_canonic_list_emits_single_named_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty canonic-ID list yields zero rows and exactly one named WARNING."""
    device = _make_phone_device(name="Lost Pixel", canonic_ids=[])
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "Lost Pixel" in warnings[0]


def test_blank_canonic_id_is_treated_as_canonicless(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A list whose only ID is a blank string is skipped and warned about once."""
    device = _make_phone_device(
        name="Blank ID Phone", canonic_ids=[SimpleNamespace(id="")]
    )
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "Blank ID Phone" in warnings[0]


def test_repeated_call_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The per-device WARNING is de-duplicated across repeated decoder calls.

    ``get_devices_with_location`` runs at least twice per poll cycle (and once per
    cycle thereafter). A persistent canonicless device must therefore warn only once.
    """
    device = _make_phone_device(name="Lost Pixel", canonic_ids=[])
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=None)
        decoder.get_devices_with_location(device_list, cache=None)

    assert len(_canonicless_warnings(caplog)) == 1


def test_valid_canonic_id_produces_row_and_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Negative control: a valid canonic ID yields one row and no canonicless WARNING."""
    device = _make_phone_device(
        name="Healthy Phone", canonic_ids=[SimpleNamespace(id="valid-canonic-id")]
    )
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert len(results) == 1
    assert _canonicless_warnings(caplog) == []

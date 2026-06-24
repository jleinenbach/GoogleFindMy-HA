# tests/test_decoder_canonicless_device_warning.py
"""Observability for devices that Google returns without a canonical ID.

When Google's device list contains a device but provides no canonical ID for it
(empty ``canonicIds`` list, or only blank/invalid IDs), ``get_devices_with_location``
emits zero rows for that device, so it silently disappears from Home Assistant. The
user has no way to tell why a phone is missing.

These tests pin a privacy-preserving two-tier signal:

* a default-visible WARNING that reports only the *count* of affected devices (never a
  user-defined device name) and is de-duplicated per config entry, and
* a DEBUG line per affected device that names it, so the operator can opt in to the
  identifying detail by enabling debug logging.

Valid devices must stay silent on both tiers (no behavioural change to the happy path).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder


@pytest.fixture(autouse=True)
def _reset_canonicless_warning_state() -> None:
    """Clear the process-wide warned-count guard before each test.

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
    """Return WARNING messages that announce a canonicless device drop (rendered)."""
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING and "canonical ID" in rec.getMessage()
    ]


def _canonicless_debug_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return DEBUG messages that name a canonicless device (rendered)."""
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.DEBUG and "canonical ID" in rec.getMessage()
    ]


def test_empty_canonic_list_emits_count_warning_without_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty canonic-ID list yields zero rows, a count WARNING and a named DEBUG line.

    The WARNING must report the count (``1``) and must NOT leak the user-defined device
    name; the name appears only at DEBUG level.
    """
    device = _make_phone_device(name="Lost Pixel", canonic_ids=[])
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []

    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "1" in warnings[0]
    assert "Lost Pixel" not in warnings[0]

    debug_lines = _canonicless_debug_lines(caplog)
    assert any("Lost Pixel" in line for line in debug_lines)


def test_blank_canonic_id_is_treated_as_canonicless(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A list whose only ID is a blank string is skipped, counted and named at DEBUG."""
    device = _make_phone_device(
        name="Blank ID Phone", canonic_ids=[SimpleNamespace(id="")]
    )
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "1" in warnings[0]
    assert "Blank ID Phone" not in warnings[0]
    assert any("Blank ID Phone" in line for line in _canonicless_debug_lines(caplog))


def test_repeated_call_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The count WARNING is de-duplicated across repeated decoder calls.

    ``get_devices_with_location`` runs at least twice per poll cycle (and once per
    cycle thereafter). A persistent canonicless count must therefore warn only once.
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
    """Negative control: a valid canonic ID yields one row and no canonicless signal."""
    device = _make_phone_device(
        name="Healthy Phone", canonic_ids=[SimpleNamespace(id="valid-canonic-id")]
    )
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert len(results) == 1
    assert _canonicless_warnings(caplog) == []
    assert _canonicless_debug_lines(caplog) == []


def test_same_count_in_distinct_entries_each_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two config entries each with a canonicless device each get their own count WARNING.

    The de-duplication guard is process-wide, so a count-only key would let the first
    entry's warning silence the second entry. The guard key must therefore include the
    per-entry scope (``cache.entry_id``).
    """
    device_list = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="Shared Name", canonic_ids=[])]
    )
    cache_a = SimpleNamespace(entry_id="entry-A")
    cache_b = SimpleNamespace(entry_id="entry-B")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)

    assert len(_canonicless_warnings(caplog)) == 2


def test_multiple_canonicless_in_one_list_warn_once_with_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two canonicless devices in one list yield a single aggregate WARNING of count 2.

    Each device is still named individually at DEBUG, but the default-visible tier is a
    single count WARNING (no per-device name, no name-collision concern).
    """
    device_list = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="Twin", canonic_ids=[]),
            _make_phone_device(name="Twin", canonic_ids=[]),
        ]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=cache)

    assert results == []
    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "2" in warnings[0]
    assert "Twin" not in warnings[0]
    # Both devices are named individually at DEBUG.
    assert len(_canonicless_debug_lines(caplog)) == 2


def test_changed_count_in_one_entry_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed canonicless count re-surfaces the WARNING (new information)."""
    one = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="A", canonic_ids=[])]
    )
    two = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="A", canonic_ids=[]),
            _make_phone_device(name="B", canonic_ids=[]),
        ]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(one, cache=cache)
        decoder.get_devices_with_location(two, cache=cache)

    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 2
    assert "1" in warnings[0]
    assert "2" in warnings[1]


def test_count_returning_to_prior_value_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A count that returns to an earlier value re-surfaces the WARNING.

    The guard remembers only the *last* count per entry, not every count ever seen.
    A cycle of 1 -> 2 -> 1 (one device recovers, leaving a different one missing, or a
    transient extra drop clears) is three distinct states, so it must warn three times.
    A lifetime set of seen counts would wrongly suppress the final ``1`` because the
    ``(entry, 1)`` key is still present from the first state.
    """
    one = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="A", canonic_ids=[])]
    )
    two = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="A", canonic_ids=[]),
            _make_phone_device(name="B", canonic_ids=[]),
        ]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(one, cache=cache)
        decoder.get_devices_with_location(two, cache=cache)
        decoder.get_devices_with_location(one, cache=cache)

    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 3
    assert "1" in warnings[0]
    assert "2" in warnings[1]
    assert "1" in warnings[2]


def test_recovery_then_redrop_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full recovery (count 0) followed by a new drop re-surfaces the WARNING.

    When an entry's affected count returns to 0 every device recovered, so the guard
    must forget that entry. A later drop is then new information and warns again, even
    if the count returns to a value seen before the recovery. The healthy (count 0)
    poll itself must stay silent on the default-visible WARNING tier.
    """
    missing = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="A", canonic_ids=[])]
    )
    healthy = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="A", canonic_ids=[SimpleNamespace(id="ok")])
        ]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(missing, cache=cache)
        decoder.get_devices_with_location(healthy, cache=cache)
        decoder.get_devices_with_location(missing, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 2


def test_same_count_in_one_entry_warns_once_across_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry-scoped count key de-duplicates a stable count across poll calls.

    A single persistent canonicless device in one entry warns only once, even though
    ``get_devices_with_location`` runs repeatedly per poll.
    """
    device_list = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="Lost Pixel", canonic_ids=[])]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache)
        decoder.get_devices_with_location(device_list, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1


def test_reset_with_entry_id_re_arms_only_that_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry-scoped reset re-arms that entry's warning without touching others.

    The guard is process-wide and survives a Home Assistant config-entry reload (the
    module is not re-imported). Without an entry-scoped reset on unload, a reload would
    leave the ``(entry, count)`` key in place, so a still-missing device would be
    silently suppressed on the next poll. ``_reset_canonicless_warning_state(entry_id)``
    must clear only that entry's keys, mirroring the per-entry isolation of the guard
    key itself (a whole-set clear would re-introduce the cross-entry coupling fixed
    earlier).
    """
    device_list = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="Lost Pixel", canonic_ids=[])]
    )
    cache_a = SimpleNamespace(entry_id="entry-A")
    cache_b = SimpleNamespace(entry_id="entry-B")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        # Both entries warn once; the guard now holds a key per entry.
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)
        # Simulate a reload of entry-A only.
        decoder._reset_canonicless_warning_state("entry-A")
        # entry-A is re-armed and warns again; entry-B stays suppressed.
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)

    assert len(_canonicless_warnings(caplog)) == 3


def test_reset_without_entry_id_clears_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reset with no entry id clears the whole guard (test-isolation contract)."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="Lost Pixel", canonic_ids=[])]
    )
    cache_a = SimpleNamespace(entry_id="entry-A")
    cache_b = SimpleNamespace(entry_id="entry-B")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)
        decoder._reset_canonicless_warning_state()
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)

    assert len(_canonicless_warnings(caplog)) == 4

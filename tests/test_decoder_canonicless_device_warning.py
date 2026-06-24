# tests/test_decoder_canonicless_device_warning.py
"""Severity of the canonicless-device signal is coupled to actionability.

When Google's device list contains a device but provides no canonical ID for it
(empty ``canonicIds`` list, or only blank/invalid IDs), ``get_devices_with_location``
emits zero rows for that device, so it silently disappears from Home Assistant.

Not every such drop is actionable, though, and a class-blind WARNING trains the
operator to ignore the signal (alert fatigue). These tests pin a severity policy that
tracks *handling relevance*:

* **Benign classes** (Android phones and reduced-listing Bluetooth accessories such as
  Pixel Buds) drop as expected for their device class, are located -- if at all -- via
  the owner's Locate flow, and therefore produce **only a DEBUG line**, never a WARNING,
  as long as they were not previously visible.
* **Warn-worthy drops** still raise the de-duplicated, count-only WARNING:
  a tracker-shaped device (has an ``information`` block, not Android) that lacks a
  canonical ID, OR any device that was visible in the previous run and now disappears
  (a real regression). The per-device name stays at DEBUG (privacy).

Valid devices stay silent on both tiers (no behavioural change to the happy path).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2, decoder


@pytest.fixture(autouse=True)
def _reset_canonicless_warning_state() -> None:
    """Clear the process-wide canonicless guards before each test.

    The decoder de-duplicates the canonicless WARNING via a module-level count map and
    tracks previously-visible device names in a second module-level map. The argument-free
    reset clears both, so every test stays order-independent.
    """
    decoder._reset_canonicless_warning_state()


def _make_phone_device(
    *,
    name: str,
    canonic_ids: list[SimpleNamespace],
) -> SimpleNamespace:
    """Build a minimal Android phone device (benign class) with a configurable ID list."""
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


def _make_buds_device(
    *,
    name: str,
    canonic_ids: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build a reduced-listing Bluetooth accessory (benign class, no information block).

    Mirrors the live Pixel Buds payload: ``identifierInformation`` plus a user-defined
    name, but no ``information`` block (so ``HasField("information")`` is False) and an
    empty generic canonic-ID list.
    """
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=DeviceUpdate_pb2.IDENTIFIER_UNKNOWN,
            canonicIds=SimpleNamespace(canonicId=canonic_ids or []),
        ),
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        HasField=lambda field_name: False,
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
            (SimpleNamespace(name="imageInformation"), None),
        ],
    )


def _make_tracker_device(
    *,
    name: str,
    canonic_ids: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    """Build a tracker-shaped device (warn-worthy class): non-Android, has an info block.

    The ``information`` substructure is present (``HasField("information")`` True) so the
    device is NOT in the benign ``not has_information_block`` class, but its nested
    ``HasField`` returns False and ``ListFields`` is empty so the DEBUG dump and the
    registration path stay inert. With ``cache=None`` no decryption is attempted.
    """
    information = SimpleNamespace(
        HasField=lambda field_name: False,
        ListFields=lambda: [],
    )
    return SimpleNamespace(
        identifierInformation=SimpleNamespace(
            type=DeviceUpdate_pb2.IDENTIFIER_SPOT,
            canonicIds=SimpleNamespace(canonicId=canonic_ids or []),
        ),
        information=information,
        userDefinedDeviceName=name,
        imageInformation=SimpleNamespace(url=""),
        HasField=lambda field_name: field_name == "information",
        ListFields=lambda: [
            (SimpleNamespace(name="identifierInformation"), None),
            (SimpleNamespace(name="information"), None),
            (SimpleNamespace(name="userDefinedDeviceName"), None),
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


# ----------------------------------------------------------------------------------------
# Benign classes (phone, accessory): silent WARNING tier, named DEBUG line, no remediation
# ----------------------------------------------------------------------------------------


def test_phone_canonicless_never_visible_is_silent_with_benign_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A never-before-visible phone with an empty canonic list warns NOT, only DEBUG (i).

    The benign DEBUG line names the device but must not carry the actionable remediation
    ("reload" / "findable again"), which would be misleading for a phone whose ID is known
    via the Locate flow.
    """
    device = _make_phone_device(name="Lost Pixel", canonic_ids=[])
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    assert _canonicless_warnings(caplog) == []

    debug_lines = _canonicless_debug_lines(caplog)
    assert any("Lost Pixel" in line for line in debug_lines)
    benign = [line for line in debug_lines if "Lost Pixel" in line]
    assert benign, "expected a benign DEBUG line naming the device"
    for line in benign:
        assert "reload" not in line
        assert "findable again" not in line


def test_buds_canonicless_never_visible_is_silent_with_benign_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A never-visible Bluetooth accessory (no information block) warns NOT, only DEBUG (ii)."""
    device = _make_buds_device(name="Pixel Buds Pro")
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    assert _canonicless_warnings(caplog) == []
    debug_lines = _canonicless_debug_lines(caplog)
    assert any("Pixel Buds Pro" in line for line in debug_lines)
    for line in [d for d in debug_lines if "Pixel Buds Pro" in d]:
        assert "reload" not in line
        assert "findable again" not in line


def test_blank_canonic_id_phone_is_silent_when_never_visible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blank-only ID list on a benign phone is treated as canonicless and stays silent."""
    device = _make_phone_device(
        name="Blank ID Phone", canonic_ids=[SimpleNamespace(id="")]
    )
    device_list = SimpleNamespace(deviceMetadata=[device])

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=None)

    assert results == []
    assert _canonicless_warnings(caplog) == []
    assert any(
        "Blank ID Phone" in line for line in _canonicless_debug_lines(caplog)
    )


def test_never_visible_benign_device_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Explicit (v): a never-visible benign device produces zero WARNING records."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_buds_device(name="Earbuds")]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache)

    assert _canonicless_warnings(caplog) == []


# ----------------------------------------------------------------------------------------
# Warn-worthy: tracker-shaped drop, and previously-visible device disappearing
# ----------------------------------------------------------------------------------------


def test_tracker_canonicless_emits_count_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tracker-shaped device without a canonical ID warns once with count 1 (iii).

    The WARNING reports the count, keeps the actionable remediation ("reload the
    integration"), and must NOT leak the device name; the name appears only at DEBUG.
    """
    device = _make_tracker_device(name="Garage Tracker")
    device_list = SimpleNamespace(deviceMetadata=[device])
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        results = decoder.get_devices_with_location(device_list, cache=cache)

    assert results == []
    warnings = _canonicless_warnings(caplog)
    assert len(warnings) == 1
    assert "1" in warnings[0]
    assert "Garage Tracker" not in warnings[0]
    assert "reload the integration" in warnings[0]
    assert any("Garage Tracker" in line for line in _canonicless_debug_lines(caplog))


def test_previously_visible_benign_device_drop_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A benign device that was visible and now drops is warn-worthy (iv).

    Even though a phone is the benign class, a real disappearance ("was visible, now
    gone") is a regression and must surface exactly one WARNING.
    """
    cache = SimpleNamespace(entry_id="entry-A")
    visible = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="X", canonic_ids=[SimpleNamespace(id="cid-x")])
        ]
    )
    dropped = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="X", canonic_ids=[])]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        first = decoder.get_devices_with_location(visible, cache=cache)
        assert len(first) == 1
        decoder.get_devices_with_location(dropped, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1


def test_previously_visible_benign_device_drop_warns_once_then_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anti-spam (iv-extended): the transition warns once, then the redrop stays silent.

    Run 1 makes "X" visible; run 2 drops it (one WARNING); run 3 drops it again. Because
    the visibility set is rebuilt per run, "X" is no longer "previously visible" in run 3,
    so it falls back to the benign class and emits no further WARNING.
    """
    cache = SimpleNamespace(entry_id="entry-A")
    visible = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="X", canonic_ids=[SimpleNamespace(id="cid-x")])
        ]
    )
    dropped = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="X", canonic_ids=[])]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(visible, cache=cache)
        decoder.get_devices_with_location(dropped, cache=cache)
        assert len(_canonicless_warnings(caplog)) == 1
        decoder.get_devices_with_location(dropped, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1


def test_transition_warns_at_most_once_per_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Multi-run-per-poll invariant (vii): two calls in one poll warn at most once.

    ``get_devices_with_location`` runs twice per poll with the same cache (api.py:361 and
    api.py:702). After "X" was visible, the first drop warns and clears "X" from the
    visibility set; the second call in the same poll no longer sees "X" as previously
    visible, so the transition warns at most once per poll.
    """
    cache = SimpleNamespace(entry_id="entry-A")
    visible = SimpleNamespace(
        deviceMetadata=[
            _make_phone_device(name="X", canonic_ids=[SimpleNamespace(id="cid-x")])
        ]
    )
    dropped = SimpleNamespace(
        deviceMetadata=[_make_phone_device(name="X", canonic_ids=[])]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(visible, cache=cache)
        decoder.get_devices_with_location(dropped, cache=cache)
        decoder.get_devices_with_location(dropped, cache=cache)

    assert len(_canonicless_warnings(caplog)) <= 1


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


# ----------------------------------------------------------------------------------------
# Count-guard re-arm mechanics, now exercised on the warn-worthy (tracker) population
# ----------------------------------------------------------------------------------------


def test_tracker_repeated_call_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The count WARNING is de-duplicated across repeated decoder calls (warn-worthy)."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_tracker_device(name="Garage Tracker")]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache)
        decoder.get_devices_with_location(device_list, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1


def test_tracker_same_count_in_distinct_entries_each_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two config entries each with a canonicless tracker each get their own WARNING."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_tracker_device(name="Shared Name")]
    )
    cache_a = SimpleNamespace(entry_id="entry-A")
    cache_b = SimpleNamespace(entry_id="entry-B")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)

    assert len(_canonicless_warnings(caplog)) == 2


def test_tracker_multiple_canonicless_in_one_list_warn_once_with_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two warn-worthy trackers in one list yield a single aggregate WARNING of count 2."""
    device_list = SimpleNamespace(
        deviceMetadata=[
            _make_tracker_device(name="Twin"),
            _make_tracker_device(name="Twin"),
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
    assert len(_canonicless_debug_lines(caplog)) == 2


def test_tracker_changed_count_in_one_entry_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A changed canonicless count re-surfaces the WARNING (new information)."""
    one = SimpleNamespace(deviceMetadata=[_make_tracker_device(name="A")])
    two = SimpleNamespace(
        deviceMetadata=[
            _make_tracker_device(name="A"),
            _make_tracker_device(name="B"),
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


def test_tracker_count_returning_to_prior_value_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A count that returns to an earlier value re-surfaces the WARNING (1 -> 2 -> 1)."""
    one = SimpleNamespace(deviceMetadata=[_make_tracker_device(name="A")])
    two = SimpleNamespace(
        deviceMetadata=[
            _make_tracker_device(name="A"),
            _make_tracker_device(name="B"),
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


def test_tracker_recovery_then_redrop_warns_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full recovery (count 0) followed by a new drop re-surfaces the WARNING."""
    missing = SimpleNamespace(deviceMetadata=[_make_tracker_device(name="A")])
    healthy = SimpleNamespace(
        deviceMetadata=[
            _make_tracker_device(name="A", canonic_ids=[SimpleNamespace(id="ok")])
        ]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(missing, cache=cache)
        decoder.get_devices_with_location(healthy, cache=cache)
        assert len(_canonicless_warnings(caplog)) == 1
        decoder.get_devices_with_location(missing, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 2


def test_mixed_run_only_benign_left_pops_guard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(vi) When a tracker recovers and only never-visible benign drops remain, count is 0.

    Run 1: a tracker drops (count 1, WARNING). Run 2: the tracker recovers and a benign,
    never-visible accessory drops -- the warn-worthy count is 0, so the guard pops and no
    second WARNING fires.
    """
    cache = SimpleNamespace(entry_id="entry-A")
    run1 = SimpleNamespace(deviceMetadata=[_make_tracker_device(name="Tracker")])
    run2 = SimpleNamespace(
        deviceMetadata=[
            _make_tracker_device(
                name="Tracker", canonic_ids=[SimpleNamespace(id="ok")]
            ),
            _make_buds_device(name="Earbuds"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(run1, cache=cache)
        assert len(_canonicless_warnings(caplog)) == 1
        decoder.get_devices_with_location(run2, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1
    assert decoder._last_canonicless_count_by_entry.get("entry-A") is None


def test_tracker_same_count_in_one_entry_warns_once_across_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry-scoped count key de-duplicates a stable count across poll calls."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_tracker_device(name="Garage Tracker")]
    )
    cache = SimpleNamespace(entry_id="entry-A")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache)
        decoder.get_devices_with_location(device_list, cache=cache)

    assert len(_canonicless_warnings(caplog)) == 1


def test_reset_with_entry_id_re_arms_only_that_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry-scoped reset re-arms that entry's warning without touching others."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_tracker_device(name="Garage Tracker")]
    )
    cache_a = SimpleNamespace(entry_id="entry-A")
    cache_b = SimpleNamespace(entry_id="entry-B")

    with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)
        decoder._reset_canonicless_warning_state("entry-A")
        decoder.get_devices_with_location(device_list, cache=cache_a)
        decoder.get_devices_with_location(device_list, cache=cache_b)

    assert len(_canonicless_warnings(caplog)) == 3


def test_reset_without_entry_id_clears_all(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reset with no entry id clears the whole guard (test-isolation contract)."""
    device_list = SimpleNamespace(
        deviceMetadata=[_make_tracker_device(name="Garage Tracker")]
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

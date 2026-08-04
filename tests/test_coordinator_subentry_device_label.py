# tests/test_coordinator_subentry_device_label.py
"""Contract tests for ``get_device_label_in_subentry``.

The accessor is the allocation-free counterpart to scanning
``get_subentry_snapshot()``: it iterates the stored tuple in place and returns
one name.  Its whole reason to exist is that it answers *exactly* what such a
scan would answer, so the tests below pin the four points where a plausible
implementation could quietly diverge: the key it matches on (``id``, never
``device_id``), the absence of a visibility gate, which row wins on duplicates,
and what happens when the stored name is not a string.
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

DEVICE_ID = "device-1"


def _coordinator(
    snapshots: dict[str, tuple[dict[str, Any], ...]] | None,
    *,
    default_key: str = TRACKER_SUBENTRY_KEY,
) -> GoogleFindMyCoordinator:
    """Return a bare coordinator carrying only what the accessor reads."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    if snapshots is not None:
        coordinator._subentry_snapshots = snapshots
    coordinator._default_subentry_key_value = default_key
    coordinator._subentry_metadata = {}
    return coordinator


def test_returns_the_stored_name_for_a_known_device() -> None:
    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": "Pixel Tag"},)}
    )

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        == "Pixel Tag"
    )


def test_none_key_falls_back_to_the_default_subentry_key() -> None:
    """``None`` resolves like the ``feature``-free branch of the snapshot getter."""

    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": "Pixel Tag"},)},
        default_key=TRACKER_SUBENTRY_KEY,
    )

    assert coordinator.get_device_label_in_subentry(None, DEVICE_ID) == "Pixel Tag"


def test_unknown_device_returns_none() -> None:
    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"id": "someone-else", "name": "Other"},)}
    )

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_unknown_subentry_key_returns_none() -> None:
    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": "Pixel Tag"},)}
    )

    assert coordinator.get_device_label_in_subentry("no-such-key", DEVICE_ID) is None


def test_row_keyed_only_by_device_id_is_not_matched() -> None:
    """``id`` only: a ``device_id``-keyed row is unreachable, as in the scan."""

    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"device_id": DEVICE_ID, "name": "Pixel Tag"},)}
    )

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_duplicate_rows_resolve_to_the_first_one() -> None:
    coordinator = _coordinator(
        {
            TRACKER_SUBENTRY_KEY: (
                {"id": DEVICE_ID, "name": "First"},
                {"id": DEVICE_ID, "name": "Second"},
            )
        }
    )

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        == "First"
    )


def test_invisible_device_still_yields_its_label() -> None:
    """No visibility gate: the accessor mirrors the snapshot, not the ACL.

    ``_subentry_metadata`` is empty, so ``is_device_visible_in_subentry``
    answers ``False`` for the very same device.  The scan this accessor replaces
    has no visibility gate either, and adding one here would be a behaviour
    change dressed up as a performance change.
    """

    coordinator = _coordinator(
        {TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": "Pixel Tag"},)}
    )

    assert not coordinator.is_device_visible_in_subentry(
        TRACKER_SUBENTRY_KEY, DEVICE_ID
    )
    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        == "Pixel Tag"
    )


def test_coordinator_without_snapshot_attribute_returns_none() -> None:
    """A ``__new__``-built double carries no ``_subentry_snapshots`` at all.

    ``get_subentry_snapshot`` raises ``AttributeError`` in that situation.  The
    accessor deliberately diverges and answers ``None``: observably the same
    "nothing happens", but without an exception that the caller's defensive
    ``except`` would swallow.
    """

    coordinator = _coordinator(None)

    assert not hasattr(coordinator, "_subentry_snapshots")
    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_non_string_name_returns_none() -> None:
    coordinator = _coordinator({TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": 42},)})

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_missing_name_key_returns_none() -> None:
    coordinator = _coordinator({TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID},)})

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_empty_snapshot_tuple_returns_none() -> None:
    coordinator = _coordinator({TRACKER_SUBENTRY_KEY: ()})

    assert (
        coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)
        is None
    )


def test_stored_row_is_not_mutated_by_the_lookup() -> None:
    """Aliasing guard: the stored row must survive a lookup unchanged.

    Note what this test is *not*: with a ``str`` return type it cannot fail, so
    it proves nothing about the current implementation and must not be counted
    as coverage of the no-aliasing requirement.  It is a standing guard for a
    later refactor that returns the row itself, where the mistake it describes
    becomes possible for the first time.
    """

    row: dict[str, Any] = {"id": DEVICE_ID, "name": "Pixel Tag", "latitude": 1.0}
    coordinator = _coordinator({TRACKER_SUBENTRY_KEY: (row,)})

    coordinator.get_device_label_in_subentry(TRACKER_SUBENTRY_KEY, DEVICE_ID)

    assert row == {"id": DEVICE_ID, "name": "Pixel Tag", "latitude": 1.0}

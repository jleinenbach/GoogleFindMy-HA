# tests/test_device_tracker_accuracy_estimated.py
"""Tests that the ``accuracy_estimated`` producer flag reaches HA attributes.

The flag is set by the cache producer and must survive the curated attribute
mapping (_as_ha_attributes) so the recorder persists it and map_view can read it
back from history. These tests pin both the True and False values (the latter
must not be dropped by the None filter) and confirm the flag is not excluded
from the recorder.
"""

from __future__ import annotations

from custom_components.googlefindmy.coordinator import _as_ha_attributes
from custom_components.googlefindmy.device_tracker import GoogleFindMyDeviceTracker


def _base_row() -> dict[str, object]:
    return {
        "name": "Phone",
        "device_id": "abc",
        "status": "Online",
        "latitude": 1.0,
        "longitude": 2.0,
        "accuracy": 200.0,
        "last_seen": 1_700_000_000,
    }


def test_as_ha_attributes_passes_estimated_true() -> None:
    """A True flag in the row is exposed in the curated attributes."""

    row = _base_row()
    row["accuracy_estimated"] = True

    result = _as_ha_attributes(row)

    assert result is not None
    assert result["accuracy_estimated"] is True


def test_as_ha_attributes_passes_estimated_false() -> None:
    """A False flag must survive the trailing None filter (False is not None)."""

    row = _base_row()
    row["accuracy"] = 12.5
    row["accuracy_estimated"] = False

    result = _as_ha_attributes(row)

    assert result is not None
    assert "accuracy_estimated" in result
    assert result["accuracy_estimated"] is False


def test_as_ha_attributes_omits_flag_when_absent() -> None:
    """Rows without the flag (legacy) do not synthesize an attribute."""

    result = _as_ha_attributes(_base_row())

    assert result is not None
    assert "accuracy_estimated" not in result


def test_accuracy_estimated_is_recorded() -> None:
    """The flag must not be excluded from the recorder.

    map_view reads ``accuracy_estimated`` from recorder history, so it must stay
    out of ``_unrecorded_attributes`` (only ``location_age`` is excluded).
    """

    assert "accuracy_estimated" not in GoogleFindMyDeviceTracker._unrecorded_attributes
    assert "location_age" in GoogleFindMyDeviceTracker._unrecorded_attributes

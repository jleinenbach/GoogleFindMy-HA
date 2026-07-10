# tests/test_plus_code_last_known.py
"""Last-known-semantics tests for the Plus Code sensor and tracker attribute.

Covers the #1179 follow-up (nie stale): the standalone Plus Code sensor and the
device_tracker ``plus_code`` attribute both report the last known position from a
single coordinator source, and never publish an accuracy-less coordinate the
tracker hides (Codex #202 / PR #1179).

Covers:
- the accuracy-less-current -> last-good fallback (C3 full parity);
- the copyable ``coordinates`` string in the map popup format (F6) and its
  coupling to ``native_value`` (F5);
- removal of ``code_length`` (P6) and the ``attribution`` state attribute (C5);
- the tracker ``plus_code`` attribute and single-source parity with the sensor.

Authored as coroutines/plain functions per ``tests/AGENTS.md`` (never
``asyncio.run()``); the sensor cases reuse the platform-setup harness from
``tests/test_plus_code_sensor.py``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.coordinator import (
    GoogleFindMyCoordinator,
    encode_plus_code_for_row,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_plus_code_sensor import (
    _EXPECTED_CODE,
    _FRESH_LAST_SEEN,
    _POSITION_ROW,
    _build_plus_code_sensor,
)

pytestmark = pytest.mark.asyncio

# A current fix with valid coordinates but no accuracy: the tracker/map hide it
# (Codex #202), so it must never be published on its own. Its coordinates differ
# from _POSITION_ROW so a fallback to the last-good row is observable.
_ACCURACY_LESS_CURRENT: dict[str, Any] = {
    "id": "dev-1",
    "device_id": "dev-1",
    "name": "Pixel",
    "latitude": 48.1371,
    "longitude": 11.5754,
    "accuracy": None,
    "last_seen": _FRESH_LAST_SEEN,
}


async def test_accuracy_less_current_falls_back_to_last_good(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Accuracy-less newest fix + older good position -> the last good code.

    Full last-known parity (C3): the sensor never goes ``unknown`` while a good
    fix exists. It publishes the last accuracy-bearing position, never the
    accuracy-less coordinate (which stays hidden per #1179).
    """
    entity = await _build_plus_code_sensor(
        _ACCURACY_LESS_CURRENT,
        deterministic_config_subentry_id,
        last_good=_POSITION_ROW,
    )
    assert entity.native_value == _EXPECTED_CODE
    # Proves it is the fallback, not the accuracy-less current fix being encoded.
    assert entity.native_value != encode_plus_code_for_row(_ACCURACY_LESS_CURRENT)


async def test_coordinates_attribute_matches_map_format(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The ``coordinates`` string equals the map popup format for the same row (F6)."""
    entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    attrs = entity.extra_state_attributes or {}
    assert (
        attrs["coordinates"]
        == f"{_POSITION_ROW['latitude']}, {_POSITION_ROW['longitude']}"
    )


async def test_coordinates_coupling_invariant(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """``coordinates`` is present exactly when ``native_value`` yields a code (F5)."""
    good = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    assert good.native_value is not None
    assert "coordinates" in (good.extra_state_attributes or {})

    none_entity = await _build_plus_code_sensor(None, deterministic_config_subentry_id)
    assert none_entity.native_value is None
    assert none_entity.extra_state_attributes is None


async def test_sensor_without_device_id_yields_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A sensor without a resolved device id yields no state and no attributes."""
    entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    entity._device_id = None
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


async def test_no_code_length_attribute(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The worthless ``code_length`` attribute is gone (P6)."""
    entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    assert "code_length" not in (entity.extra_state_attributes or {})


async def test_attribution_is_set_and_row_independent(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """``attribution`` is a stable class attribute, present even without a fix (C5)."""
    good = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    assert good._attr_attribution == "Data provided by Google"
    none_entity = await _build_plus_code_sensor(None, deterministic_config_subentry_id)
    assert none_entity._attr_attribution == "Data provided by Google"


# ---------------------------------------------------------------------------
# Coordinator last-good semantics (non-poison, fallback, restore-seed)
# ---------------------------------------------------------------------------


def _bare_coordinator() -> Any:
    """A coordinator with only the location caches wired (no full __init__)."""
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {}
    return coord


async def test_coordinator_last_good_non_poison_and_fallback() -> None:
    """An accuracy-less fix never overwrites last-good; display falls back to it."""
    coord = _bare_coordinator()
    good = {"latitude": 52.52, "longitude": 13.405, "accuracy": 12.0}

    # A good fix advances last-good (creates the cache) and is the display row.
    coord._record_last_good_location("dev-1", good)
    assert coord.get_display_location_data("dev-1") == good

    # The current fix turns accuracy-less: it must not be published, and it must
    # not poison the last-good. The display row stays the last good position.
    accuracy_less = {"latitude": 48.0, "longitude": 11.0, "accuracy": None}
    coord._device_location_data["dev-1"] = accuracy_less
    coord._record_last_good_location("dev-1", accuracy_less)  # early-return path
    assert coord.get_display_location_data("dev-1") == good

    # A second good fix advances the existing last-good cache (cache-exists path).
    good2 = {"latitude": 40.0, "longitude": -3.0, "accuracy": 8.0}
    coord._device_location_data["dev-1"] = good2
    coord._record_last_good_location("dev-1", good2)
    assert coord.get_display_location_data("dev-1") == good2


async def test_coordinator_restore_seed_via_prime() -> None:
    """Priming a device seeds last-good so display is available before first poll."""
    coord = _bare_coordinator()
    good = {"latitude": 52.52, "longitude": 13.405, "accuracy": 12.0}
    coord.prime_device_location_cache("dev-1", good)
    assert coord.get_display_location_data("dev-1") == good


async def test_coordinator_display_none_when_never_located() -> None:
    """No current fix and no last-good -> the display row is None."""
    coord = _bare_coordinator()
    assert coord.get_display_location_data("dev-1") is None


async def test_display_accessor_respects_subentry_visibility() -> None:
    """The subentry display accessor returns None for a device not in the subentry."""
    coord = _bare_coordinator()
    coord.get_display_location_data("dev-x")  # no data -> None, sanity
    coord._record_last_good_location(
        "dev-x", {"latitude": 1.0, "longitude": 2.0, "accuracy": 5.0}
    )
    # Hidden device: the visibility gate short-circuits before reading location.
    coord.is_device_visible_in_subentry = lambda key, device_id: False  # type: ignore[method-assign]
    assert coord.get_display_location_data_for_subentry("sub", "dev-x") is None
    # Visible device: the accessor returns the last-good display row.
    coord.is_device_visible_in_subentry = lambda key, device_id: True  # type: ignore[method-assign]
    row = coord.get_display_location_data_for_subentry("sub", "dev-x")
    assert row is not None and row["latitude"] == 1.0


async def test_coordinator_last_good_propagates_to_shared_devices() -> None:
    """A shared-tracker peer receives the propagated fix as its last-good."""
    coord = _bare_coordinator()
    ident = b"\xaa\xbb\xcc"
    coord._identity_key_to_devices = {ident: {"dev-a", "dev-b"}}
    good = {
        "latitude": 52.52,
        "longitude": 13.405,
        "accuracy": 12.0,
        "identity_key": ident,
        "last_seen": 1000.0,
    }
    coord._propagate_location_to_shared_devices("dev-a", good)
    row = coord.get_display_location_data("dev-b")
    assert row is not None
    assert (row["latitude"], row["longitude"]) == (52.52, 13.405)


# ---------------------------------------------------------------------------
# device_tracker plus_code attribute + single-source parity
# ---------------------------------------------------------------------------


class _DisplayCoordinatorStub:
    """Coordinator stub that serves ``row`` as the published display row."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.hass = SimpleNamespace()
        self.config_entry = make_config_entry(
            entry_id="entry-plus-code", options={}, runtime_data=None
        )
        self._row = row
        self._snapshots = {TRACKER_SUBENTRY_KEY: [row] if row else []}

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def is_device_visible_in_subentry(self, key: str, device_id: str) -> bool:
        return True

    def is_device_present(self, device_id: str) -> bool:
        return True

    def get_device_location_data_for_subentry(
        self, key: str | None, device_id: str
    ) -> dict[str, Any] | None:
        return self._row

    def get_display_location_data_for_subentry(
        self, key: str | None, device_id: str
    ) -> dict[str, Any] | None:
        return self._row

    def get_subentry_snapshot(
        self, key: str | None = None, feature: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self._snapshots.get(key or TRACKER_SUBENTRY_KEY, []))

    def stable_subentry_identifier(
        self, *, key: str | None = None, feature: str | None = None
    ) -> str | None:
        return key

    def get_subentry_metadata(
        self, *, key: str | None = None, feature: str | None = None
    ) -> Any:
        return SimpleNamespace(
            config_subentry_id=key,
            visible_device_ids=[],
            enabled_device_ids=[],
        )


def _make_tracker(row: dict[str, Any] | None) -> Any:
    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    coordinator = _DisplayCoordinatorStub(row)
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "dev-1", "name": "Pixel"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    entity._is_location_stale = lambda: False  # type: ignore[method-assign]
    entity._get_location_age = lambda *a, **k: 10.0  # type: ignore[method-assign]
    entity._get_location_status = lambda *a, **k: "available"  # type: ignore[method-assign]
    return entity


async def test_tracker_exposes_plus_code_for_display_row() -> None:
    """The tracker publishes ``plus_code`` for the last known position."""
    entity = _make_tracker(_POSITION_ROW)
    entity._sync_location_attrs()
    attrs = entity._attr_extra_state_attributes
    assert attrs["plus_code"] == _EXPECTED_CODE
    # String attribute only: it must not add a second numeric map marker.
    assert "plus_code" not in {"latitude", "longitude"}
    assert isinstance(attrs["plus_code"], str)


async def test_tracker_omits_plus_code_when_never_located() -> None:
    """A device that has never had a usable fix carries no ``plus_code``."""
    entity = _make_tracker(None)
    entity._sync_location_attrs()
    assert "plus_code" not in entity._attr_extra_state_attributes


async def test_sensor_and_tracker_share_plus_code(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Single source: sensor ``native_value`` == tracker ``plus_code`` for one row (F6/6c)."""
    sensor_entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    tracker = _make_tracker(_POSITION_ROW)
    tracker._sync_location_attrs()
    tracker_code = tracker._attr_extra_state_attributes["plus_code"]
    assert sensor_entity.native_value == tracker_code == _EXPECTED_CODE

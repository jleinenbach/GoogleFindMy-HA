# tests/test_entity_label_refresh_hotpath.py
"""Characterisation tests for ``refresh_device_label_from_coordinator``.

The method runs from five ``_handle_coordinator_update`` implementations
(``sensor.py``, ``button.py``, ``device_tracker.py``) on every coordinator
update, immediately before ``async_write_ha_state()``.  Until now it had no
test at all, so a refactor of its snapshot access had no safety net.  These
tests pin the *current* behaviour before any production change, so a later
change that alters it becomes visible as a red test rather than as a silent
semantic drift.

Scope and limits of this file (deliberate, do not read it as "the entity is
tested"):

* The instances are built with ``__new__`` and the four attributes the method
  actually reads (``coordinator``, ``_device``, ``_subentry_key``,
  ``_fallback_label``) plus the ``device_id`` property, which derives from
  ``_device["id"]``.  A full Home Assistant bootstrap is not needed and would
  contradict the "Fast" rule of ``AGENTS.md`` 3.4.
* Consequently these tests are **blind** to regressions in ``__init__``
  (``entity.py`` around the ``_subentry_key``/``_fallback_label`` assignments)
  and in ``maybe_update_device_registry_name``, which is replaced by a recorder
  on the instance.  Registry behaviour is out of scope here.
* Every case gives ``_device`` a non-empty string ``id``.  The ``device_id``
  property raises ``ValueError`` otherwise, and a minimal ``_device`` would make
  the "device missing from the snapshot" cases vacuously green: the current code
  would never enter its loop, a rewritten fast path would raise inside the
  defensive ``except`` block, and both variants would look identical.
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.entity import GoogleFindMyDeviceEntity

DEVICE_ID = "device-1"
OTHER_DEVICE_ID = "device-2"


class _SnapshotCoordinator:
    """Minimal coordinator double exposing only ``get_subentry_snapshot``."""

    def __init__(self, snapshots: dict[str, list[dict[str, Any]]]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    def get_subentry_snapshot(self, key: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(key)
        return [dict(row) for row in self._snapshots.get(key or "", [])]


def _make_entity(
    coordinator: Any,
    *,
    name: str | None = "Old name",
    fallback: str | None = "Old name",
) -> tuple[GoogleFindMyDeviceEntity, list[str | None]]:
    """Build a bare entity plus a recorder for the registry-sync call."""

    entity = GoogleFindMyDeviceEntity.__new__(GoogleFindMyDeviceEntity)
    entity.coordinator = coordinator
    entity._device = {"id": DEVICE_ID, "name": name}
    entity._subentry_key = TRACKER_SUBENTRY_KEY
    entity._fallback_label = fallback

    recorded: list[str | None] = []
    entity.maybe_update_device_registry_name = recorded.append  # type: ignore[method-assign]
    return entity, recorded


def test_changed_name_updates_device_fallback_and_registry() -> None:
    """Case 1: a new string name propagates to all three sinks."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert entity._fallback_label == "New name"
    assert recorded == ["New name"]


def test_changed_name_with_log_prefix_behaves_identically() -> None:
    """Case 1b: the optional ``log_prefix`` only adds a debug line."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator(log_prefix="[test]")

    assert entity._device["name"] == "New name"
    assert entity._fallback_label == "New name"
    assert recorded == ["New name"]


def test_unchanged_name_touches_nothing() -> None:
    """Case 2: an identical name leaves state and registry untouched."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "Old name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert recorded == []


def test_blank_name_is_ignored() -> None:
    """Case 3: empty and whitespace-only names never overwrite the label."""

    for blank in ("", "   "):
        coordinator = _SnapshotCoordinator(
            {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": blank}]}
        )
        entity, recorded = _make_entity(coordinator)

        entity.refresh_device_label_from_coordinator()

        assert entity._device["name"] == "Old name"
        assert entity._fallback_label == "Old name"
        assert recorded == []


def test_non_string_name_is_ignored() -> None:
    """Case 4: a non-``str`` name is discarded rather than coerced."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": 42}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert recorded == []


def test_device_absent_from_snapshot_keeps_current_label() -> None:
    """Case 5: a foreign row must not trigger any fallback label."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": OTHER_DEVICE_ID, "name": "Someone else"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert entity._fallback_label != GoogleFindMyDeviceEntity._DEFAULT_DEVICE_LABEL
    assert recorded == []


def test_empty_snapshot_and_unknown_key_keep_current_label() -> None:
    """Case 6: an empty snapshot and an unknown key both no-op."""

    for snapshots in ({TRACKER_SUBENTRY_KEY: []}, {"some-other-key": []}):
        coordinator = _SnapshotCoordinator(snapshots)
        entity, recorded = _make_entity(coordinator)

        entity.refresh_device_label_from_coordinator()

        assert entity._device["name"] == "Old name"
        assert entity._fallback_label == "Old name"
        assert recorded == []


def test_row_keyed_only_by_device_id_is_not_matched() -> None:
    """Case 7: the lookup keys on ``id`` alone, never on ``device_id``.

    ``coordinator/helpers/subentry.py`` groups rows by ``device_id or id``, so a
    row carrying only ``device_id`` is reachable elsewhere but invisible here.
    That asymmetry is the current behaviour and is pinned deliberately: widening
    the key would resurrect rows that are unreachable today.
    """

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"device_id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert recorded == []


def test_duplicate_rows_resolve_to_the_first_one() -> None:
    """Case 8: with two rows for one ``id``, the first row wins."""

    coordinator = _SnapshotCoordinator(
        {
            TRACKER_SUBENTRY_KEY: [
                {"id": DEVICE_ID, "name": "First"},
                {"id": DEVICE_ID, "name": "Second"},
            ]
        }
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "First"
    assert entity._fallback_label == "First"
    assert recorded == ["First"]

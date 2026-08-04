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
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
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


class _FastPathCoordinator(_SnapshotCoordinator):
    """Coordinator double implementing the allocation-free accessor as well."""

    def get_device_label_in_subentry(
        self, subentry_key: str | None, device_id: str
    ) -> str | None:
        for row in self._snapshots.get(subentry_key or "", []):
            if row.get("id") != device_id:
                continue
            name = row.get("name")
            return name if isinstance(name, str) else None
        return None


def test_fast_path_never_copies_the_snapshot() -> None:
    """Wiring proof: a coordinator with the accessor is never asked to copy.

    Asserting on the *absence* of the snapshot call is the point of this test.
    A green refresh alone would also pass if the fast path were dead code.
    """

    coordinator = _FastPathCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    for _ in range(5):
        entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert recorded == ["New name"]  # only the first pass changes anything
    assert coordinator.calls == []


def test_fast_path_is_used_even_when_the_snapshot_getter_would_raise() -> None:
    """Second wiring proof, positive this time: the label still arrives."""

    class _ExplodingSnapshot(_FastPathCoordinator):
        def get_subentry_snapshot(self, key: str | None = None) -> list[dict[str, Any]]:
            raise AssertionError("the snapshot copy must not be requested")

    coordinator = _ExplodingSnapshot(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert entity._fallback_label == "New name"
    assert recorded == ["New name"]


def test_accessor_answering_none_is_final() -> None:
    """``None`` is an answer, not a failure: it must not trigger the scan.

    A coordinator that implements the accessor and reports "no label" is
    authoritative. Falling back to the snapshot copy here would reintroduce
    exactly the allocation this change removes, on the most common path of all
    (a device whose name has not changed).
    """

    coordinator = _FastPathCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": "someone-else", "name": "Other"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert recorded == []
    assert coordinator.calls == []


def test_coordinator_without_the_accessor_still_refreshes() -> None:
    """Fallback proof: the 11 decentral test doubles keep working unchanged."""

    coordinator = _SnapshotCoordinator(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    assert not hasattr(coordinator, "get_device_label_in_subentry")
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert recorded == ["New name"]
    assert coordinator.calls == [TRACKER_SUBENTRY_KEY]


def test_raising_accessor_does_not_fall_back_to_the_scan() -> None:
    """The asymmetry is deliberate, so it is pinned rather than described.

    A *missing* accessor falls back to the scan; a *raising* one does not. It
    signals a broken coordinator, and silently scanning past that would hide
    the defect behind a working label.
    """

    class _ExplodingAccessor(_SnapshotCoordinator):
        def get_device_label_in_subentry(
            self, subentry_key: str | None, device_id: str
        ) -> str | None:
            raise RuntimeError("accessor is broken")

    coordinator = _ExplodingAccessor(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "Old name"
    assert entity._fallback_label == "Old name"
    assert recorded == []
    assert coordinator.calls == []


def test_auto_attribute_double_falls_through_to_the_scan() -> None:
    """A double whose accessor is auto-created answers with a proxy object.

    The branch exists for doubles that implement ``get_subentry_snapshot`` for
    real but grow ``get_device_label_in_subentry`` automatically: without it,
    the proxy would be mistaken for an answer and the refresh would stop.

    Measured limit, so the branch is not oversold: a plain
    ``unittest.mock.MagicMock`` is *not* rescued by it. Its
    ``get_subentry_snapshot`` yields an empty iterator, so the scan finds
    nothing and the refresh stays a no-op either way.
    """

    class _MockLike(_SnapshotCoordinator):
        def get_device_label_in_subentry(
            self, subentry_key: str | None, device_id: str
        ) -> Any:
            return object()

    coordinator = _MockLike(
        {TRACKER_SUBENTRY_KEY: [{"id": DEVICE_ID, "name": "New name"}]}
    )
    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert recorded == ["New name"]
    assert coordinator.calls == [TRACKER_SUBENTRY_KEY]


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


def test_real_coordinator_and_real_entity_use_the_accessor() -> None:
    """The seam itself, with no hand-written accessor on either side.

    Every other wiring proof in this file pairs the production entity with a
    *double* that reimplements the accessor, so all of them would stay green if
    the production accessor were renamed, removed or changed its return type:
    ``getattr`` resolves to ``None``, the entity falls back to the scan and
    nothing turns red. ``mypy`` does not close that gap either, because the
    ``getattr`` result is ``Any``.

    This test pairs the real ``GoogleFindMyCoordinator`` with the real entity
    and makes the snapshot copy explode, so the label can only arrive through
    ``SubentryOperations.get_device_label_in_subentry``.
    """

    def _explode(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("the snapshot copy must not be requested")

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._subentry_snapshots = {
        TRACKER_SUBENTRY_KEY: ({"id": DEVICE_ID, "name": "New name"},)
    }
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._subentry_metadata = {}
    coordinator.get_subentry_snapshot = _explode  # type: ignore[method-assign]

    entity, recorded = _make_entity(coordinator)

    entity.refresh_device_label_from_coordinator()

    assert entity._device["name"] == "New name"
    assert entity._fallback_label == "New name"
    assert recorded == ["New name"]

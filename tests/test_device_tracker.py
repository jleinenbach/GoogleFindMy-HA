# tests/test_device_tracker.py
"""Device tracker regression tests covering registry deduplication helpers."""

from __future__ import annotations

# tests/test_device_tracker.py
import asyncio
import importlib
import logging
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.const import (
    DEFAULT_STALE_THRESHOLD,
    DOMAIN,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator import registry as coordinator_registry
from custom_components.googlefindmy.discovery import CloudDiscoveryOutcome
from tests.helpers.config_entries_stub import make_config_entry


class _EntityRegistryStub:
    """Minimal entity registry exposing lookups used by the coordinator."""

    def __init__(self) -> None:
        self._entity_index: dict[tuple[str, str, str], str] = {}
        self.entities: dict[str, SimpleNamespace] = {}

    def add(
        self,
        *,
        entity_id: str,
        unique_id: str,
        domain: str = "device_tracker",
        platform: str = DOMAIN,
        config_entry_id: str | None = None,
    ) -> None:
        entry = SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            domain=domain,
            platform=platform,
            config_entry_id=config_entry_id,
        )
        self.entities[entity_id] = entry
        self._entity_index[(domain, platform, unique_id)] = entity_id

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        return self._entity_index.get((domain, platform, unique_id))

    def async_get(self, entity_id: str) -> SimpleNamespace | None:
        return self.entities.get(entity_id)

    def async_update_entity(self, entity_id: str, *, new_unique_id: str) -> None:
        entry = self.entities.get(entity_id)
        if entry is None:
            raise ValueError(f"Entity {entity_id} not found")

        old_key = (entry.domain, entry.platform, entry.unique_id)
        self._entity_index.pop(old_key, None)

        entry.unique_id = new_unique_id
        self._entity_index[(entry.domain, entry.platform, new_unique_id)] = entity_id


def test_find_tracker_entity_entry_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback scanning should locate tracker entries with legacy unique IDs."""

    registry = _EntityRegistryStub()
    registry.add(
        entity_id="device_tracker.googlefindmy_backpack",
        unique_id="tracker-subentry:tracker-42",
        config_entry_id="entry-42",
    )
    # Use object-based patching (er is imported in registry.py)
    monkeypatch.setattr(coordinator_registry.er, "async_get", lambda hass: registry)

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = SimpleNamespace()
    coordinator.config_entry = make_config_entry(entry_id="entry-42")
    coordinator.get_device_display_name = lambda device_id: f"Tracker {device_id}"

    entry = coordinator.find_tracker_entity_entry("tracker-42")

    assert entry is not None
    assert entry.entity_id == "device_tracker.googlefindmy_backpack"
    assert entry.unique_id.startswith("entry-42:")
    assert entry.unique_id.endswith(":tracker-42")


async def test_scanner_instantiates_tracker_for_known_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The device tracker platform should hydrate a tracker even if the registry already has it."""

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )

    async def _fake_trigger_cloud_discovery(
        *args: Any, **kwargs: Any
    ) -> CloudDiscoveryOutcome:
        # ACCEPTED is the three-state successor of the former ``True``: the
        # trigger reached a flow. This test does not assert on the outcome, so
        # only the type changes, not the statement.
        return CloudDiscoveryOutcome.ACCEPTED

    monkeypatch.setattr(
        device_tracker,
        "_trigger_cloud_discovery",
        _fake_trigger_cloud_discovery,
    )

    scheduled: list[asyncio.Task[Any]] = []

    def _async_create_task(
        coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass = SimpleNamespace(async_create_task=_async_create_task, data={})

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self.hass = hass
            self.config_entry = None
            self._listeners: list[Any] = []
            self._snapshot_calls = 0
            self.lookup_calls: list[str] = []

        def async_add_listener(self, listener):
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            return "tracker-subentry"

        def get_subentry_metadata(
            self, *, key: str | None = None, feature: str | None = None
        ) -> Any:
            return SimpleNamespace(key=TRACKER_SUBENTRY_KEY)

        def get_subentry_snapshot(
            self, key: str | None = None, *, feature: str | None = None
        ) -> list[dict[str, Any]]:
            self._snapshot_calls += 1
            if self._snapshot_calls == 1:
                return []
            return [{"id": "tracker-1", "name": "Keys"}]

        def find_tracker_entity_entry(self, device_id: str):
            self.lookup_calls.append(device_id)
            return SimpleNamespace(
                entity_id="device_tracker.googlefindmy_keys",
                unique_id="tracker-subentry:tracker-1",
            )

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-1"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}
            self._callbacks: list[Any] = []

        def async_on_unload(self, callback: Any) -> None:
            self._callbacks.append(callback)

    coordinator = _StubCoordinator()
    entry = _StubConfigEntry(coordinator)
    coordinator.config_entry = entry

    added: list[list[Any]] = []

    def _capture_entities(entities: list[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))
        assert update_before_add is True

    async def _exercise() -> None:
        await device_tracker.async_setup_entry(hass, entry, _capture_entities)
        for task in scheduled:
            await task

    await _exercise()

    # Both main tracker and last location call find_tracker_entity_entry
    assert coordinator.lookup_calls == ["tracker-1", "tracker-1"]
    # Should have 2 entities per device: main tracker + last location
    assert added and len(added[-1]) == 2
    # First entity is the main tracker
    tracker_entity = added[-1][0]
    assert tracker_entity.unique_id == "entry-1:tracker-subentry:tracker-1"
    assert tracker_entity.device_id == "tracker-1"
    # Second entity is the last location tracker
    last_location_entity = added[-1][1]
    assert (
        last_location_entity.unique_id
        == "entry-1:tracker-subentry:tracker-1:last_location"
    )
    assert last_location_entity.device_id == "tracker-1"
    assert entry._callbacks, "async_on_unload should register cleanup callbacks"
    for task in scheduled:
        assert task.done()


async def test_initial_snapshot_hydrates_registry_tracker(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Startup population should still create a tracker entity when the registry already knows it."""

    del (
        deterministic_config_subentry_id
    )  # fixture side effects patch ensure_config_subentry_id

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self.hass = SimpleNamespace(async_create_task=lambda coro: coro)
            self.config_entry = None
            self._listeners: list[Any] = []
            self.lookup_calls: list[str] = []

        def async_add_listener(self, listener):
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            return "tracker-subentry"

        def get_subentry_metadata(
            self, *, key: str | None = None, feature: str | None = None
        ) -> Any:
            return SimpleNamespace(key=TRACKER_SUBENTRY_KEY)

        def get_subentry_snapshot(
            self, key: str | None = None, *, feature: str | None = None
        ) -> list[dict[str, Any]]:
            return [{"id": "tracker-1", "name": "Keys"}]

        def find_tracker_entity_entry(self, device_id: str):
            self.lookup_calls.append(device_id)
            return SimpleNamespace(
                entity_id="device_tracker.googlefindmy_keys",
                unique_id="tracker-subentry:tracker-1",
            )

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-1"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}

        def async_on_unload(self, callback: Any) -> None:
            pass

    coordinator = _StubCoordinator()
    entry = _StubConfigEntry(coordinator)
    coordinator.config_entry = entry

    added: list[list[Any]] = []

    def _capture_entities(entities: list[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))
        assert update_before_add is True

    await device_tracker.async_setup_entry(coordinator.hass, entry, _capture_entities)

    # Should have 2 entities per device: main tracker + last location
    assert added and len(added[0]) == 2
    # First entity is the main tracker
    tracker_entity = added[0][0]
    assert tracker_entity.unique_id == "entry-1:tracker-subentry:tracker-1"
    # Second entity is the last location tracker
    last_location_entity = added[0][1]
    assert (
        last_location_entity.unique_id
        == "entry-1:tracker-subentry:tracker-1:last_location"
    )
    # Both main tracker and last location call find_tracker_entity_entry
    assert coordinator.lookup_calls == ["tracker-1", "tracker-1"]


def test_device_tracker_avoids_duplicate_accuracy_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entity updates should rely on coordinator accuracy filtering."""

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )

    class _CoordinatorStub:
        def __init__(self) -> None:
            self.hass = SimpleNamespace()
            self.config_entry = SimpleNamespace(
                entry_id="entry-accuracy", options={}, runtime_data=None
            )
            self._device_location_data: dict[
                tuple[str | None, str], dict[str, Any]
            ] = {}
            self._snapshots: dict[str, list[dict[str, Any]]] = {}

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            return lambda: None

        def is_device_visible_in_subentry(
            self, subentry_key: str, device_id: str
        ) -> bool:
            return True

        def get_device_location_data_for_subentry(
            self, key: str | None, device_id: str
        ) -> dict[str, Any] | None:
            return self._device_location_data.get((key, device_id))

        def get_display_location_data_for_subentry(
            self, key: str | None, device_id: str
        ) -> dict[str, Any] | None:
            return self.get_device_location_data_for_subentry(key, device_id)

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

        def update_location(self, key: str, device: dict[str, Any]) -> None:
            self._device_location_data[(key, device["id"])] = device
            self._snapshots[key] = [device]

    coordinator = _CoordinatorStub()
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "device-accuracy", "name": "Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()

    caplog.set_level(logging.DEBUG)
    coordinator_logger = logging.getLogger("custom_components.googlefindmy.coordinator")
    coordinator_logger.debug(
        "Dropping low-quality fix for Tracker (accuracy=150m > 100m)"
    )

    good_fix = {
        "id": "device-accuracy",
        "name": "Tracker",
        "accuracy": 25,
        "latitude": 10.0,
        "longitude": 20.0,
    }
    coordinator.update_location(TRACKER_SUBENTRY_KEY, good_fix)

    entity._handle_coordinator_update()

    assert any(
        "Dropping low-quality fix" in record.message
        for record in caplog.records
        if record.name == "custom_components.googlefindmy.coordinator"
    )
    assert all(
        "threshold" not in record.message and "accuracy=" not in record.message
        for record in caplog.records
        if record.name == "custom_components.googlefindmy.device_tracker"
    )
    assert (
        entity._last_good_accuracy_data
        == coordinator.get_device_location_data_for_subentry(
            TRACKER_SUBENTRY_KEY, "device-accuracy"
        )
    )


# ---------------------------------------------------------------------------
# Regression tests for the show_location_age option (PR #167).
# T4: default True publishes attribute, rounded to 60s; T5: False omits it;
# T6: switching the option at runtime takes effect on the next sync.
# ---------------------------------------------------------------------------


class _ShowAgeCoordinatorStub:
    """Tiny coordinator stub that lets us drive _sync_location_attrs directly."""

    def __init__(self, *, options: dict[str, Any], age_seconds: float | None) -> None:
        self.hass = SimpleNamespace()
        self.config_entry = SimpleNamespace(
            entry_id="entry-show-age", options=dict(options), runtime_data=None
        )
        self._age = age_seconds
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        self._row = {
            "id": "show-age-device",
            "device_id": "show-age-device",
            "name": "Tracker",
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 5,
        }
        self._snapshots[TRACKER_SUBENTRY_KEY] = [self._row]

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


def _make_show_age_tracker(
    coordinator: _ShowAgeCoordinatorStub, age_seconds: float | None
) -> Any:
    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "show-age-device", "name": "Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    # Bypass stale-detection and force a deterministic age value.
    entity._is_location_stale = lambda: False  # type: ignore[method-assign]
    entity._get_location_age = lambda *a, **k: age_seconds  # type: ignore[method-assign]
    entity._get_location_status = lambda *a, **k: "available"  # type: ignore[method-assign]
    return entity


def test_device_tracker_show_location_age_default_true() -> None:
    """T4: with default options the attribute is published, rounded to 60s."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=125.0)
    entity = _make_show_age_tracker(coordinator, age_seconds=125.0)

    entity._sync_location_attrs()
    attrs = entity._attr_extra_state_attributes

    assert "location_age" in attrs, "default options must publish location_age"
    # 125s rounds to nearest minute = 120s (round(125/60)=2 -> 120).
    assert attrs["location_age"] == 120
    assert isinstance(attrs["location_age"], int)


def test_device_tracker_show_location_age_false_omits_attribute() -> None:
    """T5: show_location_age=False must omit the attribute entirely."""

    coordinator = _ShowAgeCoordinatorStub(
        options={"show_location_age": False}, age_seconds=125.0
    )
    entity = _make_show_age_tracker(coordinator, age_seconds=125.0)

    entity._sync_location_attrs()
    attrs = entity._attr_extra_state_attributes

    assert "location_age" not in attrs, (
        "show_location_age=False must omit the attribute completely"
    )
    # last_seen / sensor.*_last_seen remain reachable via row data; not asserted here.


def test_device_tracker_show_location_age_runtime_toggle() -> None:
    """T6: flipping the option at runtime must drop the attribute on next sync."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=125.0)
    entity = _make_show_age_tracker(coordinator, age_seconds=125.0)

    entity._sync_location_attrs()
    assert "location_age" in entity._attr_extra_state_attributes

    # Mutate options as Home Assistant would on options-reload.
    coordinator.config_entry.options["show_location_age"] = False

    entity._sync_location_attrs()
    assert "location_age" not in entity._attr_extra_state_attributes


# ---------------------------------------------------------------------------
# Regression tests for accuracy_estimated restore (Codex review, PR #1124).
# A fallback fix recorded with accuracy_estimated=True must keep that flag when
# the device_tracker reseeds the coordinator cache on a Home Assistant restart,
# otherwise a flagless 200 m row is later reclassified as a real measurement.
# ---------------------------------------------------------------------------


class _RestoreCoordinatorStub:
    """Coordinator stub that records the cache-priming payload on restore."""

    def __init__(self) -> None:
        self.hass = SimpleNamespace()
        # Canonical config-entry stub (tests/AGENTS.md): make_config_entry
        # supplies the production data/options defaults instead of an ad-hoc
        # SimpleNamespace double. runtime_data defaults to None.
        self.config_entry = make_config_entry(entry_id="entry-restore")
        self.primed: dict[str, Any] = {}

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def prime_device_location_cache(self, device_id: str, data: dict[str, Any]) -> None:
        self.primed["device_id"] = device_id
        self.primed["data"] = dict(data)


def _build_restore_entity(
    coordinator: _RestoreCoordinatorStub,
    monkeypatch: pytest.MonkeyPatch,
    attributes: dict[str, Any],
) -> Any:
    """Build a tracker entity wired for an isolated restore round-trip."""

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "device-restore", "name": "Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace(data={DOMAIN: {}})
    # Isolate cache priming from the full attribute sync / state-write path,
    # which needs a live HA instance.
    monkeypatch.setattr(entity, "_sync_location_attrs", lambda: None)
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    restored_state = SimpleNamespace(attributes=dict(attributes))
    monkeypatch.setattr(
        entity, "async_get_last_state", AsyncMock(return_value=restored_state)
    )
    return entity


@pytest.mark.asyncio
async def test_restore_preserves_accuracy_estimated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored fallback point keeps its producer estimated flag.

    On a Home Assistant restart the device_tracker reseeds the coordinator
    cache from the last published state. A fallback fix recorded with
    ``accuracy_estimated=True`` must carry that flag into the cache so the next
    state is not reclassified as a real measurement.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 200,
            "accuracy_estimated": True,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["device_id"] == "device-restore"
    assert coordinator.primed["data"]["accuracy"] == 200
    assert coordinator.primed["data"]["accuracy_estimated"] is True
    # The entity's stale-data snapshot must stay consistent with the cache.
    assert entity._last_good_accuracy_data.get("accuracy_estimated") is True


@pytest.mark.asyncio
async def test_restore_estimated_does_not_overwrite_existing_reliable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore seed applies the same retention gate as the operational path.

    An estimated restored fix must not overwrite an already-present reliable
    last-good (defensive symmetry with the coordinator prime / operational gate,
    #1179). In the normal lifecycle restore runs on a fresh entity, so this only
    guards a re-entry, but it keeps the tracker and coordinator last-good in sync
    by construction.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 200,
            "accuracy_estimated": True,
        },
    )
    reliable = {"latitude": 52.52, "longitude": 13.405, "accuracy": 12.0}
    entity._last_good_accuracy_data = dict(reliable)

    await entity.async_added_to_hass()

    # The estimated restore is skipped: the reliable last-good survives.
    assert entity._last_good_accuracy_data == reliable


@pytest.mark.asyncio
async def test_restore_without_flag_leaves_estimated_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy rows without the flag stay unflagged on restore.

    We do not fabricate a producer flag for states recorded before the flag
    existed. The documented legacy limitation (a fallback radius is
    indistinguishable from a real measurement of the same value without the
    flag) is handled downstream by map_view's legacy fallback, not by inventing
    a value here.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 30,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 30
    assert "accuracy_estimated" not in coordinator.primed["data"]


@pytest.mark.asyncio
async def test_restore_preserves_estimated_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored real measurement keeps an explicit estimated=False flag.

    The flag must survive the restore round-trip even when it is ``False``: a
    naive truthiness filter would drop it, which would then let the downstream
    legacy fallback re-derive it. Seeding ``False`` explicitly pins the row as a
    real measurement.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 15,
            "accuracy_estimated": False,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 15
    assert coordinator.primed["data"]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_restore_stale_state_uses_accuracy_m_over_gps_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale restored state recovers its real radius from accuracy_m.

    For stale states Home Assistant clears the core latitude/longitude and omits
    ``gps_accuracy``, but the recorder still keeps the stable producer attributes
    ``accuracy_m``/``accuracy_estimated`` (and the cached lat/lon). Reading the
    value from the absent ``gps_accuracy`` alone dropped the real radius (and,
    guarded by ``acc is not None``, the flag) when reseeding the cache. The
    restore path must read the same authoritative pair as map_view and the
    snapshot builders (PR #1124 defect class, last decoupled reader).
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            # gps_accuracy intentionally absent (stale state); accuracy_m carries
            # the real 35m measurement and the producer flag says it is real.
            "accuracy_m": 35.0,
            "accuracy_estimated": False,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 35
    assert coordinator.primed["data"]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_restore_preserves_sub_meter_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restored sub-meter real fix keeps its fractional radius.

    Modern dual-frequency GNSS reports sub-meter accuracy (e.g. 0.5m). The
    restore path seeds the coordinator cache, whose ``accuracy`` field is a
    float everywhere else, so it must normalize through ``safe_accuracy`` rather
    than truncating with ``int()``. Truncating 0.5 to 0 would seed an invalid
    0m radius (below ``MIN_VALID_ACCURACY``) while still carrying
    ``accuracy_estimated=False``, recreating an invalid real fix after restart.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            # Sub-meter real measurement; must survive the restore unchanged.
            "accuracy_m": 0.5,
            "accuracy_estimated": False,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 0.5
    assert coordinator.primed["data"]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_restore_invalid_accuracy_overrides_explicit_false_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sanitized legacy error-code accuracy is marked estimated even over False.

    Codex review of PR #1126: a stale recorder row can carry the Android error
    code ``gps_accuracy=0`` (no producer ``accuracy_m``) together with an
    explicit ``accuracy_estimated=False``. ``safe_accuracy`` maps the error code
    to the 200m fallback; the canonical ``_is_significant_update`` writer always
    marks such a fabricated radius estimated regardless of an incoming flag, so
    the direct ``prime_device_location_cache`` seed must mirror that. Honoring
    the stale ``False`` would let the fabricated radius masquerade as a real
    measurement and map_view would draw a solid accuracy circle for it.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            # Android error code; no stable producer accuracy_m present.
            "gps_accuracy": 0,
            "accuracy_estimated": False,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 200.0
    assert coordinator.primed["data"]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_restore_marks_estimated_for_flagless_invalid_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flagless legacy error-code accuracy is marked estimated on restore.

    Codex review of PR #1125: a recorder row predating ``accuracy_estimated``
    can carry only the Android error code ``gps_accuracy=0`` and *no* flag.
    ``safe_accuracy`` maps it to the 200m fallback, but the direct
    ``prime_device_location_cache`` seed bypasses the canonical
    ``_is_significant_update`` writer that would pair that fallback with
    ``accuracy_estimated=True``. Without the coupling the fabricated radius
    enters flagless and map_view draws a solid accuracy circle for it. The
    restore path must mark the value estimated when the sanitization fell back
    and no explicit flag was recorded.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            # Android error code; no producer accuracy_m AND no recorded flag.
            "gps_accuracy": 0,
        },
    )

    await entity.async_added_to_hass()

    assert coordinator.primed["data"]["accuracy"] == 200.0
    assert coordinator.primed["data"]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_restore_recovers_last_seen_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore must recover last_seen so staleness survives a restart.

    Without last_seen the restored row has no age: _get_location_age() returns
    None and _is_location_stale() assumes "not stale", so a fix that predates
    the restart looks fresh until the next poll. The restore path parses the
    recorded ISO last_seen back to epoch seconds and seeds it into the cache
    row, so the recovered position is correctly aged and flagged stale.
    """

    coordinator = _RestoreCoordinatorStub()
    old_epoch = time.time() - (DEFAULT_STALE_THRESHOLD + 5000)
    old_iso = (
        datetime.fromtimestamp(old_epoch, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 50,
            "last_seen": old_iso,
        },
    )

    await entity.async_added_to_hass()

    seeded = coordinator.primed["data"]
    # last_seen is recovered as an epoch float within a second of the source.
    assert seeded["last_seen"] == pytest.approx(old_epoch, abs=1.0)
    assert entity._last_good_accuracy_data["last_seen"] == pytest.approx(
        old_epoch, abs=1.0
    )
    # Behavioural consequence: the restored row carries its true age and the
    # status derived from it is "stale" (not the age-less "unknown").
    age = entity._get_location_age(seeded)
    assert age is not None and age > DEFAULT_STALE_THRESHOLD
    assert entity._get_location_status(seeded) == "stale"


@pytest.mark.asyncio
async def test_restore_recovers_last_seen_from_utc_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``last_seen`` is absent, the restore falls back to ``last_seen_utc``.

    Both keys carry the same ISO timestamp; only one may be present in a given
    recorded state. The fallback keeps the recovered age correct in either case.
    """

    coordinator = _RestoreCoordinatorStub()
    old_epoch = time.time() - (DEFAULT_STALE_THRESHOLD + 5000)
    old_iso = (
        datetime.fromtimestamp(old_epoch, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 50,
            # No "last_seen"; only the UTC mirror is recorded.
            "last_seen_utc": old_iso,
        },
    )

    await entity.async_added_to_hass()

    seeded = coordinator.primed["data"]
    assert seeded["last_seen"] == pytest.approx(old_epoch, abs=1.0)
    assert entity._get_location_status(seeded) == "stale"


@pytest.mark.asyncio
async def test_restore_without_timestamp_leaves_last_seen_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded state without any timestamp seeds no last_seen (old behavior).

    With neither ``last_seen`` nor ``last_seen_utc`` present, the restore must
    not fabricate an age: the key stays absent and the age remains unknown.
    """

    coordinator = _RestoreCoordinatorStub()
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            "latitude": 10.0,
            "longitude": 20.0,
            "gps_accuracy": 50,
            # No last_seen / last_seen_utc at all.
        },
    )

    await entity.async_added_to_hass()

    seeded = coordinator.primed["data"]
    assert "last_seen" not in seeded
    assert entity._get_location_age(seeded) is None


@pytest.mark.asyncio
async def test_restore_falls_back_to_recorder_only_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state recorded while stale withholds the plain latitude/longitude keys.

    The producer strips them so HA does not republish a withheld position as the
    live GPS attribute (Codex #1177) and keeps the last known fix in the
    recorder-only last_latitude/last_longitude keys. Restore must fall back to
    those so a restart still recovers the position instead of coming up empty.
    """

    coordinator = _RestoreCoordinatorStub()
    old_epoch = time.time() - (DEFAULT_STALE_THRESHOLD + 5000)
    old_iso = (
        datetime.fromtimestamp(old_epoch, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    entity = _build_restore_entity(
        coordinator,
        monkeypatch,
        {
            # No plain latitude/longitude: this state was stale at record time,
            # so the producer stripped them. Only the recorder-only keys carry
            # the last known fix.
            "last_latitude": 10.0,
            "last_longitude": 20.0,
            "accuracy_m": 50,
            "last_seen": old_iso,
        },
    )

    await entity.async_added_to_hass()

    seeded = coordinator.primed["data"]
    # Position recovered via the recorder-only fallback keys.
    assert seeded["latitude"] == 10.0
    assert seeded["longitude"] == 20.0
    # And the recovered fix still carries its true (stale) age.
    assert seeded["last_seen"] == pytest.approx(old_epoch, abs=1.0)


# ---------------------------------------------------------------------------
# Regression tests for the stale-threshold retune (spurious "unknown" flapping)
# and the latent accuracy-less-fallback bug B1 (PR #201 follow-up audit).
#
# Root cause: a stationary device at home (dozing smartphone, or a tag scanned
# only by the household's own dozing phones) reports via FMDN roughly every
# ~30 min. The previous 1800 s default sat exactly on that cadence, so any
# delayed report tipped an otherwise-present device into "stale" and blanked
# its coordinates. B1 is the sibling defect: a fresh row with valid lat/lon but
# no accuracy was blanked instead of falling back to the last accuracy-bearing
# fix, and the "last good accuracy" cache was overwritten by accuracy-less
# rows.
# ---------------------------------------------------------------------------


def _make_stale_probe_tracker(coordinator: _ShowAgeCoordinatorStub) -> Any:
    """Build a tracker with the REAL stale methods (no overrides), so the
    default threshold actually governs the stale decision."""

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "show-age-device", "name": "Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    return entity


def test_home_cadence_report_not_stale_under_default_threshold() -> None:
    """A ~35 min old fix (typical home reporting cadence) must NOT be stale
    under the retuned default. Under the previous 1800 s default the very same
    age would have flipped to "stale" and blanked the coordinates."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=None)
    entity = _make_stale_probe_tracker(coordinator)
    # 2100 s = 35 min: above the old 1800 s default, below the new 3900 s one.
    entity._get_location_age = lambda *a, **k: 2100.0  # type: ignore[method-assign]

    assert entity._get_stale_threshold() == DEFAULT_STALE_THRESHOLD
    assert DEFAULT_STALE_THRESHOLD > 1800, "default must be retuned above 1800 s"
    assert entity._is_location_stale() is False, (
        "a 35 min old home report must not be treated as stale"
    )


def test_accuracy_less_fresh_row_falls_back_to_cached_fix() -> None:
    """B1 read path: a fresh row with valid lat/lon but no accuracy must fall
    back to the last accuracy-bearing fix instead of blanking coordinates."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=100.0)
    # Fresh current row: valid coordinates, but the accuracy key is absent.
    coordinator._row = {
        "id": "show-age-device",
        "device_id": "show-age-device",
        "name": "Tracker",
        "latitude": 11.0,
        "longitude": 21.0,
        "last_seen": time.time(),
    }
    entity = _make_show_age_tracker(coordinator, age_seconds=100.0)
    # Prime the cache with a good, accuracy-bearing fix.
    entity._last_good_accuracy_data = {
        "latitude": 10.0,
        "longitude": 20.0,
        "accuracy": 5,
        "last_seen": time.time() - 100,
    }

    entity._sync_location_attrs()

    # Coordinates come from the cached accuracy-bearing fix, not nulled.
    assert entity._attr_latitude == 10.0
    assert entity._attr_longitude == 20.0
    assert entity._attr_location_accuracy == 5.0


def test_accuracy_less_update_does_not_poison_accuracy_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 write path: an accuracy-less update must NOT overwrite the cached
    accuracy-bearing fix (the cache is _last_good_ACCURACY_data)."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=100.0)
    coordinator._row = {
        "id": "show-age-device",
        "device_id": "show-age-device",
        "name": "Tracker",
        "latitude": 11.0,
        "longitude": 21.0,
        "last_seen": time.time(),
    }
    entity = _make_show_age_tracker(coordinator, age_seconds=100.0)
    good = {
        "latitude": 10.0,
        "longitude": 20.0,
        "accuracy": 5,
        "last_seen": time.time() - 100,
    }
    entity._last_good_accuracy_data = dict(good)
    # Isolate the cache-update branch of _handle_coordinator_update.
    monkeypatch.setattr(entity, "_sync_location_attrs", lambda: None)
    monkeypatch.setattr(entity, "async_write_ha_state", lambda: None)
    monkeypatch.setattr(
        entity, "refresh_device_label_from_coordinator", lambda **kw: None
    )

    entity._handle_coordinator_update()

    assert entity._last_good_accuracy_data == good, (
        "an accuracy-less update must not poison the accuracy cache"
    )


def _make_plain_tracker(coordinator: _ShowAgeCoordinatorStub) -> Any:
    """Build a tracker WITHOUT monkeypatching age/status/stale helpers.

    Used to assert the real derivation ties every published value to the same
    display row (Codex #202), which the show-age helper cannot verify because it
    stubs those methods out.
    """
    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    entity = device_tracker.GoogleFindMyDeviceTracker(
        coordinator,
        {"id": "show-age-device", "name": "Tracker"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=TRACKER_SUBENTRY_KEY,
    )
    entity.hass = SimpleNamespace()
    return entity


def test_accuracy_less_row_ties_all_metadata_to_display_row() -> None:
    """Codex #202: when coordinates fall back to the cached accuracy-bearing
    fix, the age, status and extra attributes must describe that SAME cached
    row, never the accuracy-less current row. Otherwise a moving device is shown
    at cached coordinates with a fresh age and attributes exposing a different
    lat/lon than the tracker properties."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=None)
    # Fresh current row: NEW lat/lon and a brand-new last_seen, but no accuracy.
    coordinator._row = {
        "id": "show-age-device",
        "device_id": "show-age-device",
        "name": "Tracker",
        "latitude": 11.0,
        "longitude": 21.0,
        "last_seen": time.time(),
    }
    entity = _make_plain_tracker(coordinator)
    # Cache: the last accuracy-bearing fix, ~2000 s old, at DIFFERENT
    # coordinates. 2000 s sits in the "aging" band (> half the 3900 s default,
    # below the full threshold) so its status differs from the fresh current
    # row's "current" -- this makes the test sharp against age/status still
    # being read from the current row.
    entity._last_good_accuracy_data = {
        "latitude": 10.0,
        "longitude": 20.0,
        "accuracy": 5,
        "last_seen": time.time() - 2000,
    }

    entity._sync_location_attrs()
    attrs = entity._attr_extra_state_attributes

    # The liveness gate stays non-stale (current row is fresh), so coordinates
    # are still published -- from the cached accuracy-bearing fix.
    assert entity._attr_latitude == 10.0
    assert entity._attr_longitude == 20.0
    # Extra attributes must expose the SAME cached position, not the current
    # row's 11.0/21.0 (this is the divergence Codex flagged).
    assert attrs.get("latitude") == 10.0
    assert attrs.get("longitude") == 20.0
    # Age/status describe the cached row (~2000 s -> "aging"), NOT the fresh
    # current row (which would read "current"/age 0).
    assert attrs["location_status"] == "aging"
    assert attrs["location_age"] == 1980  # round(2000/60)*60


def test_stale_branch_strips_live_coordinate_keys() -> None:
    """Codex #1177: a stale state must NOT leave the plain latitude/longitude keys
    in extra_state_attributes. HA merges extra_state_attributes last and exposes
    those keys as the device_tracker GPS attributes, so keeping them would
    republish the withheld (stale) position as the live location for
    closest()/proximity/the built-in map. The last known fix is exposed via the
    recorder-only last_latitude/last_longitude keys instead; accuracy_m is not an
    HA GPS attribute and stays for restore/history/snapshot."""

    coordinator = _ShowAgeCoordinatorStub(options={}, age_seconds=None)
    coordinator._row = {
        "id": "show-age-device",
        "device_id": "show-age-device",
        "name": "Tracker",
        "latitude": 12.0,
        "longitude": 22.0,
        "accuracy": 8,
        "last_seen": time.time() - 10000,
    }
    entity = _make_plain_tracker(coordinator)
    entity._is_location_stale = lambda: True  # type: ignore[method-assign]

    entity._sync_location_attrs()
    attrs = entity._attr_extra_state_attributes

    # Live tracker coordinates are withheld while stale.
    assert entity._attr_latitude is None
    assert entity._attr_longitude is None
    # The plain GPS keys must be stripped so HA does not republish the stale
    # position as the live location (the #1177 leak).
    assert "latitude" not in attrs
    assert "longitude" not in attrs
    # accuracy_m is not an HA GPS attribute; it stays for restore/history.
    assert attrs["accuracy_m"] == 8.0
    # The last known position is exposed via the recorder-only keys instead.
    assert attrs["last_latitude"] == 12.0
    assert attrs["last_longitude"] == 22.0

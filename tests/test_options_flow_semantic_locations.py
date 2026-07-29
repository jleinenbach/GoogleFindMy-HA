# tests/test_options_flow_semantic_locations.py
"""Options flow coverage for the semantic-location steps."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    DEFAULT_SEMANTIC_DETECTION_RADIUS,
    OPT_SEMANTIC_LOCATIONS,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import prepare_flow_hass_config_entries


class _SemanticConfigEntries:
    """Record options updates and reloads for verification."""

    def __init__(self, entry: SimpleNamespace) -> None:
        self._entry = entry
        self.updated_options: list[dict[str, Any]] = []
        self.reloaded: list[str] = []
        self.scheduled_reloads: list[str] = []

    def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

    def async_update_entry(
        self, entry: SimpleNamespace, *, options: dict[str, Any] | None = None
    ) -> None:
        assert entry is self._entry
        if options is not None:
            self.updated_options.append(options)
            entry.options = options

    def async_schedule_reload(self, entry_id: str) -> None:
        """Record the call; deliberately do not chain into ``async_reload``.

        Synchronous on purpose: ``ConfigEntries.async_schedule_reload`` is a
        ``@callback`` returning ``None``, and a coroutine here would make the
        production call site look awaitable when it is not.

        Two core behaviours are left out knowingly, so nothing here is mistaken
        for a faithful replica: the core raises ``UnknownEntry`` for an entry id
        it does not know, and it hands the reload to ``async_create_task``. This
        recorder does neither, which is what lets an assertion tell the two call
        styles apart -- but it also means the failure branch of
        ``_schedule_claimed_reload`` cannot be reached from this file.
        """

        self.scheduled_reloads.append(entry_id)

    async def async_reload(self, entry_id: str) -> None:
        """Kept next to :meth:`async_schedule_reload` as a regression tripwire.

        A later fall back to the awaited variant would otherwise pass silently;
        with both recorders present the assertions can tell the two apart.
        """

        self.reloaded.append(entry_id)


class _FakeState:
    """Simple state stub exposing attributes."""

    def __init__(self, attributes: Mapping[str, Any]) -> None:
        self.attributes = attributes


class _FakeStates:
    """Lookup helper for zone state retrieval."""

    def __init__(self, mapping: dict[str, _FakeState]) -> None:
        self._mapping = mapping

    def get(self, entity_id: str) -> _FakeState | None:
        return self._mapping.get(entity_id)


class _HassStub:
    """Minimal Home Assistant stub for semantic options flows."""

    def __init__(self, entry: SimpleNamespace, *, home_radius: float = 90.0) -> None:
        self.config_entries = _SemanticConfigEntries(entry)
        prepare_flow_hass_config_entries(
            self, lambda: self.config_entries, frame_module=frame
        )
        self.config = SimpleNamespace(latitude=12.5, longitude=34.5)
        self.states = _FakeStates(
            {
                "zone.home": _FakeState(
                    {"latitude": 56.0, "longitude": 78.0, "radius": home_radius}
                )
            }
        )
        self.data: dict[str, Any] = {}
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(
        self, coro: Awaitable[Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def drain_tasks(self) -> None:
        if not self._tasks:
            return
        await asyncio.gather(*self._tasks)


@pytest.mark.asyncio
async def test_semantic_locations_options_lifecycle() -> None:
    """Options flow should add, guard, and remove semantic locations."""

    entry = make_config_entry(
        entry_id="entry-semantic",
        title="Semantic",
        options={
            OPT_SEMANTIC_LOCATIONS: {
                "Office": {"latitude": 1.0, "longitude": 2.0, "accuracy": 3.0}
            }
        },
    )
    hass = _HassStub(entry)

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    init_result = await flow.async_step_init()
    assert init_result["type"] == "menu"
    assert "semantic_locations" in init_result["menu_options"]

    add_form = await flow.async_step_semantic_locations_add(None)
    defaults: dict[str, float | str] = {}
    for marker in add_form["data_schema"].schema:
        default_factory = marker.default
        defaults[marker.schema] = (
            default_factory()
            if isinstance(default_factory, Callable)
            else default_factory
        )
    assert defaults == {
        "semantic_name": "",
        "latitude": 56.0,
        "longitude": 78.0,
        "accuracy": 90.0,
    }

    initial_options = entry.options
    add_result = await flow.async_step_semantic_locations_add(
        {
            "semantic_name": "Park",
            "latitude": 56.0,
            "longitude": 78.0,
            "accuracy": 45.0,
        }
    )
    await hass.drain_tasks()

    assert add_result["type"] == "menu"
    assert hass.config_entries.updated_options[0] is not initial_options
    assert hass.config_entries.updated_options[0][OPT_SEMANTIC_LOCATIONS]["Park"] == {
        "latitude": 56.0,
        "longitude": 78.0,
        "accuracy": 45.0,
    }
    assert hass.config_entries.reloaded == [entry.entry_id]

    duplicate = await flow.async_step_semantic_locations_add(
        {
            "semantic_name": "park",
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 1.0,
        }
    )
    assert duplicate["errors"] == {"semantic_name": "duplicate_semantic_location"}

    delete_form = await flow.async_step_semantic_locations_delete(None)
    selected = {marker.schema for marker in delete_form["data_schema"].schema}
    assert "semantic_locations" in selected

    delete_result = await flow.async_step_semantic_locations_delete(
        {"semantic_locations": ["Park"]}
    )
    await hass.drain_tasks()

    assert delete_result["type"] == "menu"
    assert hass.config_entries.updated_options[-1][OPT_SEMANTIC_LOCATIONS] == {
        "Office": {"latitude": 1.0, "longitude": 2.0, "accuracy": 3.0}
    }
    assert hass.config_entries.reloaded == [entry.entry_id, entry.entry_id]


@pytest.mark.asyncio
async def test_semantic_location_defaults_floor_accuracy() -> None:
    """Defaults should treat semantic detections as broad (>=50m) receivers."""

    entry = make_config_entry(entry_id="entry-semantic", title="Semantic")
    hass = _HassStub(entry, home_radius=10.0)

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    add_form = await flow.async_step_semantic_locations_add(None)
    defaults: dict[str, float | str] = {}
    for marker in add_form["data_schema"].schema:
        default_factory = marker.default
        defaults[marker.schema] = (
            default_factory()
            if isinstance(default_factory, Callable)
            else default_factory
        )

    assert defaults == {
        "semantic_name": "",
        "latitude": 56.0,
        "longitude": 78.0,
        "accuracy": DEFAULT_SEMANTIC_DETECTION_RADIUS,
    }


@pytest.mark.asyncio
async def test_semantic_location_edit_prefills_existing_values() -> None:
    """Editing should default to the stored semantic location coordinates."""

    entry = make_config_entry(
        entry_id="entry-semantic",
        title="Semantic",
        options={
            OPT_SEMANTIC_LOCATIONS: {
                "Büro": {"latitude": 50.0, "longitude": 10.0, "accuracy": 7.0}
            }
        },
    )
    hass = _HassStub(entry)

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    edit_form = await flow.async_step_semantic_locations_edit(
        {"semantic_location": "Büro"}
    )
    defaults: dict[str, float | str] = {}
    for marker in edit_form["data_schema"].schema:
        default_factory = marker.default
        defaults[marker.schema] = (
            default_factory()
            if isinstance(default_factory, Callable)
            else default_factory
        )

    assert defaults == {
        "semantic_name": "Büro",
        "latitude": 50.0,
        "longitude": 10.0,
        "accuracy": 7.0,
    }


def test_the_scheduling_double_matches_the_core_call_shape() -> None:
    """The recorder has to be reachable and synchronous, or AP4 debugs a typo.

    Nothing in this file asserts on ``scheduled_reloads`` yet -- the options
    steps still take the awaited route. Without this check a misspelled method
    name would stay invisible here and only surface one work package later, as a
    production helper silently taking its "no lever" branch.
    """

    entry = make_config_entry(entry_id="entry-shape", title="Shape")
    manager = _SemanticConfigEntries(entry)

    schedule = getattr(manager, "async_schedule_reload", None)
    assert callable(schedule)
    assert not inspect.iscoroutinefunction(schedule), (
        "the core's async_schedule_reload is a @callback; an awaitable double "
        "would hide that the production call site does not await it"
    )

    schedule(entry.entry_id)
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloaded == [], (
        "the two recorders have to stay distinguishable; that is the whole "
        "point of keeping async_reload next to it"
    )

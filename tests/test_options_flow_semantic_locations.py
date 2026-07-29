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
    assert hass.config_entries.scheduled_reloads == [entry.entry_id]
    assert hass.config_entries.reloaded == []

    duplicate = await flow.async_step_semantic_locations_add(
        {
            "semantic_name": "park",
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 1.0,
        }
    )
    assert duplicate["errors"] == {"semantic_name": "duplicate_semantic_location"}

    # The add above claimed the shared latch, and in production the scheduled
    # reload hands it back when it arrives (release points: unload and setup).
    # Nothing schedules a real reload here, so the release is replayed by hand.
    # Without it the delete below would stand down -- correctly, but for a reason
    # that exists only in this stub, and the second user action would go
    # unmeasured.
    config_flow.import_integration_package().discard_pending_entry_reload(
        hass, entry.entry_id
    )

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
    # Two entries, not one: adding and deleting are two separate user actions,
    # each owning its own reload, with the replayed release in between standing
    # in for the reload that a running core would have delivered.
    assert hass.config_entries.scheduled_reloads == [entry.entry_id, entry.entry_id]
    assert hass.config_entries.reloaded == []


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
    """The recorder has to be reachable and synchronous, or a typo hides here.

    The tests above now assert on ``scheduled_reloads``, so a misspelled method
    name would no longer pass unnoticed. What stays worth pinning is the *shape*:
    an awaitable double would let the production call site look awaitable when it
    is not, and a recorder that quietly chained into ``async_reload`` would make
    the two routes indistinguishable.
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


# --- AP4: the semantic steps stand behind the single reload owner ------------


def _latch_is_free(hass: Any, entry_id: str) -> bool:
    """Whether the shared reload latch of ``entry_id`` can be claimed again.

    Claims the latch as a side effect, so this belongs at the end of a test.
    Same shape as the helper in ``tests/test_config_flow_reload_latch_state_guard.py``.
    """

    integration = config_flow.import_integration_package()
    return bool(integration.claim_pending_entry_reload(hass, entry_id))


def _claim_latch(hass: Any, entry_id: str) -> None:
    """Take the shared latch on behalf of a foreign reload owner."""

    integration = config_flow.import_integration_package()
    assert integration.claim_pending_entry_reload(hass, entry_id) is True


def _semantic_flow(hass: Any, entry: Any) -> Any:
    """An options flow bound to ``hass`` and ``entry``."""

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow


@pytest.mark.asyncio
async def test_a_delete_schedules_exactly_one_reload() -> None:
    """Test A: the normal path owns its reload and schedules it once."""

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
    flow = _semantic_flow(hass, entry)

    result = await flow.async_step_semantic_locations_delete(
        {"semantic_locations": ["Office"]}
    )

    assert result["type"] == "menu"
    assert hass.config_entries.scheduled_reloads == [entry.entry_id]
    assert hass.config_entries.reloaded == []


@pytest.mark.asyncio
async def test_a_foreign_owner_makes_the_delete_stand_down_but_not_skip() -> None:
    """Test B: standing down never costs the write.

    The second assertion is the point of the whole work package: the options are
    in the entry before the claim, so the foreign reload picks them up. A version
    that stood down *before* writing would pass the first assertion and lose the
    user's deletion.
    """

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
    _claim_latch(hass, entry.entry_id)
    flow = _semantic_flow(hass, entry)

    result = await flow.async_step_semantic_locations_delete(
        {"semantic_locations": ["Office"]}
    )

    assert result["type"] == "menu"
    assert hass.config_entries.scheduled_reloads == []
    assert hass.config_entries.reloaded == []
    assert entry.options[OPT_SEMANTIC_LOCATIONS] == {}


@pytest.mark.asyncio
async def test_a_core_without_the_lever_leaves_the_latch_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test C: no lever, no reload -- and above all no burnt latch.

    An implementation that claimed first and only then noticed the missing
    ``async_schedule_reload`` would leave the latch set for the lifetime of the
    process and swallow every later reload of this entry.
    """

    entry = make_config_entry(
        entry_id="entry-semantic",
        title="Semantic",
        options={
            OPT_SEMANTIC_LOCATIONS: {
                "Office": {"latitude": 1.0, "longitude": 2.0, "accuracy": 3.0}
            }
        },
    )
    # Removed on the class, not shadowed on the instance: the production helper
    # resolves the lever with ``getattr(..., None)``, and an instance attribute
    # set to ``None`` would exercise the same branch for the wrong reason. A core
    # from before ``async_schedule_reload`` simply does not carry the method.
    monkeypatch.delattr(_SemanticConfigEntries, "async_schedule_reload")
    hass = _HassStub(entry)
    flow = _semantic_flow(hass, entry)

    result = await flow.async_step_semantic_locations_delete(
        {"semantic_locations": ["Office"]}
    )

    assert result["type"] == "menu"
    assert hass.config_entries.scheduled_reloads == []
    assert hass.config_entries.reloaded == []
    assert entry.options[OPT_SEMANTIC_LOCATIONS] == {}
    assert _latch_is_free(hass, entry.entry_id) is True

# tests/test_options_flow_subentries.py

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    OPT_CONTRIBUTOR_MODE,
    OPT_IGNORED_DEVICES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_flow import prepare_flow_hass_config_entries

pytestmark = pytest.mark.asyncio


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for options tests."""

    return f"{entry_id}-{key}-subentry"


@dataclass
class _ManagerStub:
    """Minimal config_entries manager capturing subentry operations."""

    entry: _EntryStub

    def __post_init__(self) -> None:
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []
        self.reloads: list[str] = []
        self.scheduled_reloads: list[str] = []
        self.setup_calls: list[str] = []

    def async_update_entry(self, entry: _EntryStub, *, data: dict[str, Any]) -> None:
        assert entry is self.entry
        entry.data = data

    def async_update_subentry(  # noqa: PLR0913
        self,
        entry: _EntryStub,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        assert entry is self.entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        if translation_key is not None:
            subentry.translation_key = translation_key
        self.updated.append((subentry.subentry_id, dict(subentry.data)))

    def async_remove_subentry(self, entry: _EntryStub, subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self.entry
        entry.subentries.pop(subentry_id, None)
        self.removed.append(subentry_id)
        return True

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

        Latent coupling, deliberately named here: ``_core_auto_schedules_subentries``
        only consults this attribute once the manager also exposes
        ``async_setup_subentry`` **and** ``async_get_subentries``. This stub has the
        second but not the first, so the probe still answers ``False``. Adding
        ``async_setup_subentry`` here later would flip that probe silently.
        """

        self.scheduled_reloads.append(entry_id)

    async def async_reload(self, entry_id: str) -> None:
        """Kept next to :meth:`async_schedule_reload` as a regression tripwire.

        A later fall back to the awaited variant would otherwise pass silently;
        with both recorders present the assertions can tell the two apart.
        """

        self.reloads.append(entry_id)

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self.entry.entry_id:
            return self.entry
        return None

    def async_get_subentries(self, entry_id: str) -> list[ConfigSubentry]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        return list(entry.subentries.values())

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


class _EntryStub:
    """Config entry stub exposing subentries and mutable options."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.title = "Entry Title"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=[]))
        # The three fields the reload gate in ``_schedule_claimed_reload``
        # reads. Without them every ``getattr(entry, ..., None)`` in
        # ``_entry_reload_is_hopeless`` falls back to ``None``, the gate can
        # only ever answer "not hopeless", and a test that claims to exercise
        # the gate would in fact exercise nothing (fail-open, so silently).
        # Defaults are the healthy case; a test that wants the gate to bite
        # assigns a terminal ``state`` (see the guard test at the end of this
        # file). ``ConfigEntryState`` is imported from the submodule because
        # that is the module world the production code compares against.
        self.state = ConfigEntryState.LOADED
        self.source = "user"
        self.disabled_by = None

    def add_subentry(
        self,
        *,
        key: str,
        title: str,
        visible_device_ids: list[str] | None = None,
        feature_flags: dict[str, Any] | None = None,
    ) -> ConfigSubentry:
        payload = {
            "group_key": key,
            "feature_flags": feature_flags or {},
        }
        if visible_device_ids is not None:
            payload["visible_device_ids"] = list(visible_device_ids)
        subentry = ConfigSubentry(
            data=payload,
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title=title,
            unique_id=f"{self.entry_id}-{key}",
            subentry_id=_stable_subentry_id(self.entry_id, key),
            translation_key=key,
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        # Load-bearing since the repair steps schedule through
        # ``_schedule_claimed_reload``: the tests at the end of this file read
        # and pre-set the latch through it. The
        # reload latch lives in ``hass.data``: without this mapping
        # ``_domain_data`` raises ``AttributeError`` inside
        # ``_claim_entry_reload``, which swallows it and fails open with ``True``.
        # The stand-down branch would then be unreachable and its mutation probe
        # meaningless.
        self.data: dict[str, Any] = {}

    @classmethod
    async def create(cls, entry: _EntryStub) -> _HassStub:
        hass = cls(entry)
        prepare_flow_hass_config_entries(
            hass,
            lambda: _ManagerStub(entry),
            frame_module=frame,
        )
        return hass

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


async def _build_flow(entry: _EntryStub) -> config_flow.OptionsFlowHandler:
    flow = config_flow.OptionsFlowHandler()
    flow.hass = await _HassStub.create(entry)  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow


async def test_settings_updates_feature_flags_for_selected_subentry() -> None:
    """Settings step should persist feature flags to the chosen subentry."""

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(
        {
            "subentry": TRACKER_SUBENTRY_KEY,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            OPT_CONTRIBUTOR_MODE: "high_traffic",
        }
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    _, payload = manager.updated[-1]
    assert payload["feature_flags"][OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert payload["feature_flags"][OPT_CONTRIBUTOR_MODE] == "high_traffic"


async def test_visibility_assigns_devices_to_target_subentry() -> None:
    """Visibility step should attach restored devices to the chosen subentry."""

    entry = _EntryStub()
    sub = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {
        OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}},
    }

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated_id, payload = manager.updated[-1]
    assert updated_id == sub.subentry_id
    assert payload["visible_device_ids"] == ("dev-1",)
    # Assert both sides (``tests/AGENTS.md`` rule 8): the step routes through
    # ``_schedule_claimed_reload`` like its two sibling assignment sites, so the
    # scheduling side has to be claimed and the direct side has to stay empty.
    # Unignoring without a reload leaves the device without any entity, because
    # the platform known-sets never drop an id -- see the comment at the call
    # site and the platform-level characterisation in
    # ``test_device_tracker_scanner.py``.
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_repairs_move_assigns_devices_to_selected_subentry() -> None:
    """Repair move step should remove devices from other subentries."""

    entry = _EntryStub()
    target = entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    other = entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-2"])
    entry.runtime_data.coordinator.data = [
        {"device_id": "dev-1", "name": "Device 1"},
        {"device_id": "dev-2", "name": "Device 2"},
    ]

    flow = await _build_flow(entry)

    async def _invoke() -> dict[str, Any]:
        result = await flow.async_step_repairs_move(
            {"target_subentry": "target", "device_ids": ["dev-1", "dev-2"]}
        )
        await asyncio.sleep(0)
        return result

    result = await _invoke()

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[target.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
    assert tuple(updated[other.subentry_id]["visible_device_ids"]) == ()
    # Test D: exactly one reload, and it goes through the scheduling lever.
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_repairs_delete_moves_devices_and_removes_subentry() -> None:
    """Deleting a subentry moves devices to fallback and removes the source."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-1", "dev-2"]
    )
    fallback = entry.add_subentry(key="keep", title="Keep", visible_device_ids=[])

    flow = await _build_flow(entry)

    async def _invoke_delete() -> dict[str, Any]:
        result = await flow.async_step_repairs_delete(
            {"delete_subentry": "remove", "fallback_subentry": "keep"}
        )
        await asyncio.sleep(0)
        return result

    result = await _invoke_delete()

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert removable.subentry_id in manager.removed
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[fallback.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_the_scheduling_double_matches_the_core_call_shape() -> None:
    """The recorder has to be reachable and synchronous, or a typo hides here.

    Declared ``async`` only because this module carries a file-wide
    ``pytestmark = pytest.mark.asyncio``; the check itself needs no event loop.

    The repair tests now assert on ``scheduled_reloads``, so a misspelled method
    name no longer passes unnoticed. What stays worth pinning is the *shape*: an
    awaitable double would let the production call site look awaitable when it is
    not, and a recorder chaining into ``async_reload`` would blur the two routes.
    """

    entry = _EntryStub()
    manager = _ManagerStub(entry)

    schedule = getattr(manager, "async_schedule_reload", None)
    assert callable(schedule)
    assert not inspect.iscoroutinefunction(schedule), (
        "the core's async_schedule_reload is a @callback; an awaitable double "
        "would hide that the production call site does not await it"
    )

    schedule(entry.entry_id)
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == [], (
        "the two recorders have to stay distinguishable; that is the whole "
        "point of keeping async_reload next to it"
    )


# --- AP5: the repair steps stand behind the single reload owner --------------


def _claim_latch(hass: Any, entry_id: str) -> None:
    """Take the shared latch on behalf of a foreign reload owner."""

    integration = config_flow.import_integration_package()
    assert integration.claim_pending_entry_reload(hass, entry_id) is True


def _latch_is_free(hass: Any, entry_id: str) -> bool:
    """Whether the shared reload latch of ``entry_id`` can be claimed again.

    Claims the latch as a side effect, so this belongs at the end of a test.
    """

    integration = config_flow.import_integration_package()
    return bool(integration.claim_pending_entry_reload(hass, entry_id))


async def test_repairs_move_stands_down_but_still_writes_the_assignment() -> None:
    """Test E: a foreign owner costs the reload, never the assignment."""

    entry = _EntryStub()
    target = entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-1"])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)
    _claim_latch(flow.hass, entry.entry_id)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[target.subentry_id]["visible_device_ids"]) == ("dev-1",)


async def test_repairs_delete_stands_down_but_still_removes_the_subentry() -> None:
    """Test F: the removal happens before the claim and survives it."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-1"]
    )
    entry.add_subentry(key="keep", title="Keep", visible_device_ids=[])

    flow = await _build_flow(entry)
    _claim_latch(flow.hass, entry.entry_id)

    result = await flow.async_step_repairs_delete(
        {"delete_subentry": "remove", "fallback_subentry": "keep"}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_delete_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert removable.subentry_id in manager.removed


async def test_a_move_that_changes_nothing_does_not_burn_the_latch() -> None:
    """Test G: an ineffective step schedules nothing and leaves the latch free.

    The second assertion is the one that matters. A claim pulled in front of the
    ``if not changed`` exit would satisfy the first (nothing is scheduled on that
    branch anyway) while leaving the latch set for the lifetime of the process,
    swallowing every later reload of this entry.
    """

    entry = _EntryStub()
    entry.add_subentry(key="target", title="Target", visible_device_ids=["dev-1"])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True


async def test_a_hopeless_entry_stops_the_repair_step_from_claiming_the_latch() -> None:
    """Test H: the state gate is reachable from this file, and it bites.

    Tests D to G above exercise the latch half of ``_schedule_claimed_reload``
    while the state gate in front of it can only ever answer "not hopeless",
    because ``_EntryStub`` now carries healthy values for the three fields it
    reads. That makes those tests honest but leaves the gate itself unmeasured
    from here -- and the gate is the reason the repair steps were routed through
    the single owner at all. This test closes that hole for the repair path: an
    entry in a terminal state must not have its latch claimed, because no setup
    would follow to hand it back, and the leaked latch would then swallow every
    later reload of this entry.

    Removing ``_entry_reload_is_hopeless`` from ``_schedule_claimed_reload``
    turns the first two assertions red; the third one guards against "fixing"
    that by skipping the write as well.
    """

    entry = _EntryStub()
    entry.state = ConfigEntryState.MIGRATION_ERROR
    entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True
    # The step still did its job: standing down is about the reload, never
    # about the change the user asked for.
    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    assert "dev-1" in entry.subentries[next(iter(entry.subentries))].data.get(
        "visible_device_ids", []
    )


async def test_visibility_without_a_selection_does_not_schedule_a_reload() -> None:
    """Test I: closing the form without restoring anything must stay inert.

    The claim sits inside ``if to_restore:`` for this reason. Pulled in front of
    it, every visit to the visibility page that the user simply confirms would
    reload the entry, and a claim that no assignment justifies would still occupy
    the single owner for the duration of that reload.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": []}
    )
    await asyncio.sleep(0)

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True


async def test_a_hopeless_entry_stops_the_visibility_step_from_claiming(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test J: the state gate is reachable from the visibility step too.

    Test H does this for the repair path. The visibility step took its claim
    later and would otherwise be the one assignment site whose gate nobody
    measures: a terminal entry has no setup coming that would hand the latch
    back, so a claim taken here would swallow every later reload of this entry.
    Two assertions guard against "fixing" a red gate by skipping the user's
    change as well -- standing down is about the reload, never about the
    assignment.

    The last one pins the warning. Here a stand-down costs the whole thing, not
    a half: the restored device has no entity to fall back on, so this branch
    has to say so in the log rather than return a silent success.
    """

    entry = _EntryStub()
    entry.state = ConfigEntryState.MIGRATION_ERROR
    sub = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )
    await asyncio.sleep(0)

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    # Both sides (rule 8): without the second one a later rewrite that swaps
    # ``_schedule_claimed_reload`` for a direct ``async_reload`` would bypass the
    # state gate and still leave this test green.
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True
    assert result["type"] == "create_entry"
    updated_id, payload = manager.updated[-1]
    assert updated_id == sub.subentry_id
    assert payload["visible_device_ids"] == ("dev-1",)
    assert any(
        record.levelname == "WARNING" and "no reload was scheduled" in record.message
        for record in caplog.records
    )

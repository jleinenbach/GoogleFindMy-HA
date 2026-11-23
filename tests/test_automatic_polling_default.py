# tests/test_automatic_polling_default.py
"""Automatic location polling is off for a tracker without a stored choice.

Covers the stored choice, the one-time update of entries from before this
default, and the Repairs notice that tells the user how many trackers are no
longer polled automatically after that update.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.repairs import ConfirmRepairFlow

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import const, repairs
from custom_components.googlefindmy.const import DOMAIN
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_coordinator_automatic_polling import (
    _core_entry,
    _CoreLikeConfigEntries,
    _switch_coordinator,
)
from tests.test_remove_entry_cleanup import (  # noqa: F401 - fixture import
    _HassStub,
    _no_fcm_release,
    _setup_runtime,
)

_OPT_DISABLED = "automatic_polling_disabled"
_OPT_ENABLED = "automatic_polling_enabled"
_OPT_CHANGED_AT = "automatic_polling_changed_at"
_ENTRY_ID = "entry-default"
_STAMP = "2026-11-10T20:00:00Z"
_NOTICE_ID = f"automatic_polling_default_off_{_ENTRY_ID}"


def _migrate(options: dict[str, Any]) -> Any:
    """Run the start-up migration on an entry with the core's write semantics."""
    entry = make_config_entry(entry_id=_ENTRY_ID, options=options)
    hass = SimpleNamespace(config_entries=_CoreLikeConfigEntries())
    integration._migrate_entry_identifier_namespaces(hass, entry)
    return entry


class _Registries:
    """Device and entity registry rows of one entry, read by the notice count.

    The shared registry stubs of ``tests/conftest.py`` carry no ``disabled_by``
    on entities; these rows carry exactly the fields the count reads.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.devices: list[SimpleNamespace] = []
        self.entities: list[SimpleNamespace] = []
        monkeypatch.setattr(integration.dr, "async_get", lambda _hass: self)
        monkeypatch.setattr(integration.er, "async_get", lambda _hass: self)
        monkeypatch.setattr(
            integration.dr,
            "async_entries_for_config_entry",
            lambda _reg, _entry_id: list(self.devices),
        )
        monkeypatch.setattr(
            integration.er,
            "async_entries_for_config_entry",
            lambda _reg, _entry_id: list(self.entities),
        )

    def device(
        self,
        identifier: tuple[str, str],
        *,
        device_disabled: bool = False,
        tracker: bool = True,
    ) -> None:
        reg_id = f"reg-{len(self.devices)}"
        self.devices.append(
            SimpleNamespace(
                id=reg_id,
                identifiers={identifier},
                disabled_by="user" if device_disabled else None,
            )
        )
        if tracker:
            self.entities.append(
                SimpleNamespace(
                    platform=DOMAIN,
                    domain="device_tracker",
                    disabled_by=None,
                    device_id=reg_id,
                )
            )

    def tracker(self, device_id: str, **kwargs: Any) -> None:
        self.device((DOMAIN, f"{_ENTRY_ID}:{device_id}"), **kwargs)


class _Entry:
    """A config entry with its unload callbacks, as in the frozen-attribute tests."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.disabled_by: str | None = None
        self.on_unload: list[Any] = []

    def async_on_unload(self, func: Any) -> None:
        self.on_unload.append(func)

    def unload(self) -> None:
        for func in self.on_unload:
            func()
        self.on_unload.clear()


# ---------------------------------------------------------------------------
# (c) (d): the stored choice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_off_a_tracker_without_a_choice_is_stored(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) "Off" for a tracker that is off only for lack of a choice is stored.

    Without the stored "off" the tracker would turn on as soon as a later
    version changed the default again; the user's choice must survive that.
    """
    entry = _core_entry(hass, {})
    coord = _switch_coordinator(monkeypatch, hass, entry)
    assert coord.is_automatic_polling_enabled("dev-off") is False

    coord.async_set_automatic_polling("dev-off", False)

    assert entry.options.get(_OPT_DISABLED) == ["dev-off"], (
        "turning off a tracker without a choice is stored"
    )
    assert entry.options[_OPT_ENABLED] == []
    assert entry.options[_OPT_CHANGED_AT]["dev-off"]


@pytest.mark.asyncio
async def test_both_lists_never_hold_one_id(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) Each switch moves the id from one list to the other, never into both."""
    entry = _core_entry(hass, {})
    coord = _switch_coordinator(monkeypatch, hass, entry)

    for enabled in (True, False, True):
        coord.async_set_automatic_polling("dev-on", enabled)
        on_list = set(entry.options[_OPT_ENABLED])
        off_list = set(entry.options[_OPT_DISABLED])
        assert not on_list & off_list
        assert ("dev-on" in on_list) is enabled
        assert coord.is_automatic_polling_enabled("dev-on") is enabled

    # A stored conflict from elsewhere is repaired at start-up: the off list wins.
    repaired = _migrate({_OPT_ENABLED: ["dev-x", "dev-y"], _OPT_DISABLED: ["dev-x"]})
    assert repaired.options[_OPT_ENABLED] == ["dev-y"]
    assert repaired.options[_OPT_DISABLED] == ["dev-x"]


# ---------------------------------------------------------------------------
# (e) (e2): the one-time update of an entry from before this default
# ---------------------------------------------------------------------------


def test_update_keeps_trackers_turned_back_on() -> None:
    """(e) A tracker switched off and on again before the update stays on.

    Its change time is stored without an entry in the off list. A tracker that
    is off stays off, and an entry-scoped key counts as the canonical id.
    """
    entry = _migrate(
        {
            _OPT_DISABLED: ["dev-off"],
            _OPT_CHANGED_AT: {
                f"{_ENTRY_ID}:dev-back-on": _STAMP,
                "dev-off": _STAMP,
            },
        }
    )

    assert entry.options[_OPT_ENABLED] == ["dev-back-on"]
    assert entry.options[_OPT_DISABLED] == ["dev-off"]


def test_update_runs_once_per_entry(
    monkeypatch: pytest.MonkeyPatch, issue_registry_capture: Any
) -> None:
    """(e2) The update runs once: the stored on list marks it as done.

    After the first run the key is written to the entry. On a later start the
    stored on list is left alone even with a change time that the update would
    turn into "on", and no notice comes back although a tracker has no choice.
    """
    first = _migrate({_OPT_CHANGED_AT: {"dev-x": _STAMP}, _OPT_DISABLED: ["dev-x"]})
    assert first.options[_OPT_ENABLED] == []

    _Registries(monkeypatch).tracker("dev-untouched")
    entry = _migrate({_OPT_ENABLED: [], _OPT_CHANGED_AT: {"dev-x": _STAMP}})

    assert entry.options[_OPT_ENABLED] == []
    assert issue_registry_capture.created == []
    assert const.OPT_AUTOMATIC_POLLING_ENABLED in const.OPTION_KEYS
    assert const.OPT_AUTOMATIC_POLLING_ENABLED not in const.DEFAULT_OPTIONS


# ---------------------------------------------------------------------------
# (g): the notice counts the trackers that lost automatic polling
# ---------------------------------------------------------------------------


def test_update_notice_counts_trackers_without_a_choice(
    monkeypatch: pytest.MonkeyPatch, issue_registry_capture: Any
) -> None:
    """(g) Only enabled trackers without a choice count; a new entry gets no notice.

    Not counted: the service device, an ignored tracker, a disabled device, and
    trackers whose choice is stored as on or off.
    """
    registries = _Registries(monkeypatch)
    registries.device(const.service_device_identifier(_ENTRY_ID))
    for device_id in ("dev-untouched", "dev-ignored", "dev-off", "dev-back-on"):
        registries.tracker(device_id)
    registries.tracker("dev-disabled", device_disabled=True)
    registries.tracker("dev-without-tracker", tracker=False)

    _migrate(
        {
            const.OPT_IGNORED_DEVICES: ["dev-ignored"],
            _OPT_DISABLED: ["dev-off"],
            _OPT_CHANGED_AT: {"dev-back-on": _STAMP, "dev-off": _STAMP},
        }
    )

    assert [item["issue_id"] for item in issue_registry_capture.created] == [_NOTICE_ID]
    notice = issue_registry_capture.created[0]
    assert notice["translation_placeholders"] == {"count": "1"}
    assert notice["translation_key"] == "automatic_polling_default_off"
    assert notice["is_fixable"] is True
    assert notice["is_persistent"] is True

    # A new entry: its setup runs before the platforms, so no device exists yet.
    registries.devices.clear()
    registries.entities.clear()
    fresh = _migrate({})
    assert fresh.options[_OPT_ENABLED] == []
    assert len(issue_registry_capture.created) == 1


# ---------------------------------------------------------------------------
# (i1) to (i4): how the notice goes away
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_notice_closes_with_got_it(hass: Any) -> None:
    """(i1) The notice opens a confirmation, not the re-authentication flow."""
    flow = await repairs.async_create_fix_flow(hass, _NOTICE_ID, None)

    assert isinstance(flow, ConfirmRepairFlow)


def test_update_notice_goes_when_the_entry_is_disabled(
    issue_registry_capture: Any,
) -> None:
    """(i2) Disabling the entry deletes its notice; another entry keeps its own."""
    hass = SimpleNamespace()
    kept, disabled = _Entry("entry-a"), _Entry("entry-b")
    for entry in (kept, disabled):
        integration._async_create_automatic_polling_notice(hass, entry, 2)
        integration._register_automatic_polling_notice_cleanup(hass, entry)

    disabled.disabled_by = "user"
    disabled.unload()

    assert issue_registry_capture.deleted == [
        (DOMAIN, "automatic_polling_default_off_entry-b")
    ]
    assert set(issue_registry_capture.registry.issues) == {
        "automatic_polling_default_off_entry-a"
    }


def test_update_notice_survives_a_reload(issue_registry_capture: Any) -> None:
    """(i3) A plain unload (reload, shutdown) keeps the notice; so does the next one."""
    hass = SimpleNamespace()
    entry = _Entry("entry-a")
    integration._async_create_automatic_polling_notice(hass, entry, 3)
    integration._register_automatic_polling_notice_cleanup(hass, entry)

    entry.unload()
    integration._register_automatic_polling_notice_cleanup(hass, entry)
    entry.unload()

    assert issue_registry_capture.deleted == []
    assert set(issue_registry_capture.registry.issues) == {
        "automatic_polling_default_off_entry-a"
    }


@pytest.mark.asyncio
async def test_update_notice_goes_with_the_entry(
    monkeypatch: pytest.MonkeyPatch,
    issue_registry_capture: Any,
    _no_fcm_release: list[None],  # noqa: F811 - fixture
) -> None:
    """(i4) Removing the entry deletes its notice."""
    monkeypatch.setattr(integration, "_unregister_instance", lambda _entry_id: None)
    entry = make_config_entry(entry_id="entry-remove", title="Find My Entry")
    _coordinator, _cache, _filter, runtime_data = _setup_runtime(entry)
    hass = _HassStub(entry, runtime_data)

    await integration.async_remove_entry(hass, entry)

    assert (
        DOMAIN,
        "automatic_polling_default_off_entry-remove",
    ) in issue_registry_capture.deleted

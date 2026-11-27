# tests/test_automatic_polling_notice.py
"""Notification when automatic location polling is turned on for a device.

Turning the switch *Automatic location polling* on shows one Home Assistant
notification that explains what the requests do on the device: a phone listed
without a canonical ID gets the phone text, a phone listed with a canonical ID
gets nothing, every other device gets the tracker text. Turning the switch off
and removing the device remove it. Neither a restart nor the setup of the
switch creates one: the coordinator's setter is the only place that shows it.

``async_create`` and ``async_dismiss`` are replaced on the module
``homeassistant.components.persistent_notification`` itself, so a call from any
module of the integration is counted, not only one from the coordinator.

New names of the feature are looked up with ``getattr`` and a ``pytest.fail``
instead of an import, so that on the commit before the feature this module is
collected and fails test by test instead of erroring at collection.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components import persistent_notification
from homeassistant.helpers import translation

import custom_components.googlefindmy as integration
from custom_components.googlefindmy import switch
from custom_components.googlefindmy.const import DOMAIN, TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_coordinator_automatic_polling import (
    _CoreLikeConfigEntries,
    _DispatchingAPI,
    _make_coordinator,
)

_OPT_DISABLED = "automatic_polling_disabled"
_OPT_ENABLED = "automatic_polling_enabled"
_ENTRY_ID = "entry-notice"
_TRACKER = "dev-tag"
_TRACKER_NAME = "Blue Backpack"
_PHONE = "aphone-" + "0123456789abcdef" * 2
_PHONE_NAME = "Second Phone"
_CANONIC_PHONE = "dev-phone"
_CANONIC_PHONE_NAME = "Main Phone"
_INTEGRATION_ROOT = Path(__file__).resolve().parents[1] / (
    "custom_components/googlefindmy"
)
_README = Path(__file__).resolve().parents[1] / "README.md"
_KEYS = (
    "automatic_polling_notice_title",
    "automatic_polling_notice_phone",
    "automatic_polling_notice_tracker",
)
_EN = json.loads(
    (_INTEGRATION_ROOT / "translations" / "en.json").read_text(encoding="utf-8")
)["config"]["progress"]


def _require(owner: Any, name: str) -> Any:
    """Return ``owner.name`` or fail the test: the feature is not there yet."""
    value = getattr(owner, name, None)
    if value is None:
        pytest.fail(f"{type(owner).__name__} has no {name}")
    return value


def _notice_id(device_id: str) -> str:
    return f"{DOMAIN}_automatic_polling_{_ENTRY_ID}_{device_id}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Notices:
    """Records ``async_create`` and ``async_dismiss`` on the core module."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.created: list[dict[str, Any]] = []
        self.dismissed: list[str] = []
        monkeypatch.setattr(persistent_notification, "async_create", self._create)
        monkeypatch.setattr(persistent_notification, "async_dismiss", self._dismiss)

    def _create(
        self,
        _hass: Any,
        message: str,
        title: str | None = None,
        notification_id: str | None = None,
    ) -> None:
        self.created.append(
            {"message": message, "title": title, "notification_id": notification_id}
        )

    def _dismiss(self, _hass: Any, notification_id: str) -> None:
        self.dismissed.append(notification_id)


def _english_texts(
    monkeypatch: pytest.MonkeyPatch, *, while_loading: Any = None
) -> list[str]:
    """Serve the English texts as ``async_get_translations`` would.

    ``while_loading`` runs once, inside the first lookup, where the real lookup
    awaits its files. The returned list records the requested languages.
    """
    languages: list[str] = []
    pending_hook = [while_loading] if while_loading is not None else []

    async def _lookup(
        _hass: Any, language: str, category: str, integrations: Any = None
    ) -> dict[str, str]:
        languages.append(language)
        assert category == "component"
        assert set(integrations or ()) == {DOMAIN}
        if pending_hook:
            pending_hook.pop()()
        await asyncio.sleep(0)
        return {
            f"component.{DOMAIN}.config.progress.{key}": value
            for key, value in _EN.items()
        }

    monkeypatch.setattr(translation, "async_get_translations", _lookup)
    return languages


class _Hass:
    """Core manager semantics for the options, tasks kept until drained."""

    def __init__(self) -> None:
        self.config_entries = _CoreLikeConfigEntries()
        self.config = SimpleNamespace(language="en")
        self.tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    async def drain(self) -> None:
        """Await the planned tasks; a task that keeps planning new ones fails."""
        for _ in range(20):
            if not self.tasks:
                return
            pending = list(self.tasks)
            self.tasks.clear()
            await asyncio.gather(*pending)
        for task in self.tasks:
            task.cancel()
        pytest.fail("planned tasks keep planning new tasks")


def _bare_coordinator(
    *, phone_route: frozenset[str] = frozenset(), rows: dict[str, Any] | None = None
) -> GoogleFindMyCoordinator:
    """The production class without ``__init__``, with the fields the notice reads."""
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord.hass = _Hass()  # type: ignore[assignment]
    coord.config_entry = make_config_entry(
        entry_id=_ENTRY_ID, options={_OPT_DISABLED: [], _OPT_ENABLED: []}
    )
    coord.api = SimpleNamespace(has_phone_target=lambda dev: dev in phone_route)
    coord._device_location_data = dict(rows or {})
    names = {
        _TRACKER: _TRACKER_NAME,
        _PHONE: _PHONE_NAME,
        _CANONIC_PHONE: _CANONIC_PHONE_NAME,
    }
    coord.get_device_display_name = names.get  # type: ignore[method-assign]
    return coord


async def _set(coord: GoogleFindMyCoordinator, device_id: str, enabled: bool) -> None:
    _require(coord, "async_set_automatic_polling")(device_id, enabled)
    await coord.hass.drain()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Which device gets which notice, and when it goes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_on_creates_one_notice_for_a_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One notice with the fixed id, the name in the title, the tracker text."""
    notices = _Notices(monkeypatch)
    languages = _english_texts(monkeypatch)
    coord = _bare_coordinator()

    await _set(coord, _TRACKER, True)

    assert len(notices.created) == 1, notices.created
    created = notices.created[0]
    assert created["notification_id"] == _notice_id(_TRACKER)
    assert created["title"] == _EN["automatic_polling_notice_title"].replace(
        "{device}", _TRACKER_NAME
    )
    assert _TRACKER_NAME in created["title"]
    assert created["message"] == _EN["automatic_polling_notice_tracker"]
    assert languages == ["en"]


@pytest.mark.asyncio
async def test_switch_on_phone_without_canonic_id_gets_phone_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone listed without a canonical ID gets the phone text."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator(phone_route=frozenset({_PHONE}))

    await _set(coord, _PHONE, True)

    assert [c["message"] for c in notices.created] == [
        _EN["automatic_polling_notice_phone"]
    ]
    assert notices.created[0]["notification_id"] == _notice_id(_PHONE)


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["phone_route", "device_type"])
async def test_switch_on_phone_with_canonic_id_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, way: str
) -> None:
    """A phone listed with a canonical ID gets no notice.

    ``phone_route``: the device list offers the phone locate action.
    ``device_type``: the current row carries ``device_type`` 20 (phone).
    """
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    if way == "phone_route":
        coord = _bare_coordinator(phone_route=frozenset({_CANONIC_PHONE}))
    else:
        coord = _bare_coordinator(rows={_CANONIC_PHONE: {"device_type": 20}})

    await _set(coord, _CANONIC_PHONE, True)

    assert coord.config_entry.options[_OPT_ENABLED] == [_CANONIC_PHONE]
    assert notices.created == []


@pytest.mark.asyncio
async def test_switch_on_twice_replaces_not_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On to on creates nothing; on, off, on uses the same id both times."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()

    await _set(coord, _TRACKER, True)
    await _set(coord, _TRACKER, True)
    assert len(notices.created) == 1, "on to on must not create a second notice"

    await _set(coord, _TRACKER, False)
    await _set(coord, _TRACKER, True)
    assert [c["notification_id"] for c in notices.created] == [
        _notice_id(_TRACKER),
        _notice_id(_TRACKER),
    ]


@pytest.mark.asyncio
async def test_switch_off_dismisses_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turning the switch off removes the device's notice."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()

    await _set(coord, _TRACKER, True)
    await _set(coord, _TRACKER, False)

    assert notices.dismissed == [_notice_id(_TRACKER)]


@pytest.mark.asyncio
async def test_removing_device_dismisses_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``purge_device`` (device deletion flow) removes the device's notice.

    Built through the real constructor: ``purge_device`` clears many stores,
    which a coordinator from ``__new__`` lacks.
    """
    notices = _Notices(monkeypatch)
    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    coord.config_entry = make_config_entry(entry_id=_ENTRY_ID, options={})
    coord.async_update_listeners = lambda: None  # type: ignore[method-assign]
    coord.async_set_updated_data = lambda _data: None  # type: ignore[method-assign]
    coord.data = []

    coord.purge_device(_TRACKER)

    assert notices.dismissed == [_notice_id(_TRACKER)]
    assert notices.created == []


# ---------------------------------------------------------------------------
# Only the setter creates a notice
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_with_switch_on_creates_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start with the switch on creates no notice.

    The start-up migration runs on the stored choice "on", the coordinator is
    built through its real constructor, and the switch entity is added to Home
    Assistant with ``is_on`` true.
    """
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    entry = make_config_entry(
        entry_id=_ENTRY_ID,
        options={_OPT_ENABLED: ["dev-on"], _OPT_DISABLED: []},
    )
    hass = _Hass()
    integration._migrate_entry_identifier_namespaces(hass, entry)

    coord = _make_coordinator(
        monkeypatch, options=dict(entry.options), api=_DispatchingAPI()
    )
    # Real hass has a configured language; without it a notice planned by the
    # constructor would die on the missing attribute instead of being counted.
    coord.hass.config = SimpleNamespace(language="en")  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    coord.config_entry = entry
    entity = switch.GoogleFindMyAutomaticPollingSwitch(
        coord,
        {"id": "dev-on", "name": "Keys"},
        None,
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier="sub-tracker",
    )
    entity.hass = coord.hass
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    await entity.async_added_to_hass()
    # A notice planned anywhere (migration, constructor, entity) runs as a task
    # on this loop; wait for every task but this test before asserting.
    await hass.drain()
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if others:
        await asyncio.wait(others, timeout=5)
    failed = [
        repr(t.exception())
        for t in others
        if t.done() and not t.cancelled() and t.exception() is not None
    ]
    assert failed == [], "a task died, so the count below would measure nothing"

    assert entity.is_on is True
    assert notices.created == []


def _package_trees() -> list[tuple[str, ast.Module]]:
    return [
        (
            path.relative_to(_INTEGRATION_ROOT).as_posix(),
            ast.parse(path.read_text(encoding="utf-8")),
        )
        for path in sorted(_INTEGRATION_ROOT.rglob("*.py"))
    ]


def _calls_with_function(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    """Every call in ``tree`` with the name of the function around it."""
    found: list[tuple[ast.Call, str]] = []

    def _walk(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call):
                found.append((child, owner))
            _walk(child, name)

    _walk(tree, "<module>")
    return found


def _notification_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to the core module and to its ``async_create``."""
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound = alias.asname or alias.name
                if (
                    node.module == "homeassistant.components"
                    and alias.name == "persistent_notification"
                ):
                    modules.add(bound)
                if (
                    node.module == "homeassistant.components.persistent_notification"
                    and alias.name == "async_create"
                ):
                    functions.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "homeassistant.components.persistent_notification":
                    modules.add(alias.asname or alias.name)
    return modules, functions


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def test_notice_created_only_from_the_switch_setter() -> None:
    """One way to a notice: switch entity -> setter -> show -> core create."""
    shows: list[tuple[str, str]] = []
    creates: list[tuple[str, str]] = []
    setters: list[tuple[str, str]] = []
    for relative, tree in _package_trees():
        modules, functions = _notification_aliases(tree)
        for call, owner in _calls_with_function(tree):
            func = call.func
            target = _dotted(func)
            if isinstance(func, ast.Attribute):
                if func.attr == "_async_show_automatic_polling_notice":
                    shows.append((relative, owner))
                if func.attr == "async_set_automatic_polling":
                    setters.append((relative, owner))
                if func.attr == "async_create" and _dotted(func.value) in modules:
                    creates.append((relative, owner))
            elif isinstance(func, ast.Name) and target in functions:
                creates.append((relative, owner))

    assert shows == [("coordinator/main.py", "async_set_automatic_polling")], shows
    assert creates == [
        ("coordinator/main.py", "_async_show_automatic_polling_notice")
    ], creates
    assert sorted(setters) == [
        ("switch.py", "async_turn_off"),
        ("switch.py", "async_turn_on"),
    ], setters


# ---------------------------------------------------------------------------
# Text loading, translations, logging and the README
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_before_text_loaded_leaves_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turned off while the text loads: the notice is not created."""
    notices = _Notices(monkeypatch)
    coord = _bare_coordinator()
    _english_texts(
        monkeypatch,
        while_loading=lambda: coord.async_set_automatic_polling(_TRACKER, False),
    )

    await _set(coord, _TRACKER, True)

    assert coord.config_entry.options[_OPT_DISABLED] == [_TRACKER]
    assert notices.created == []


def test_notice_texts_in_all_languages_without_interval_values() -> None:
    """Eleven files, three keys each, ``{device}`` in the title, no digit."""
    files = [_INTEGRATION_ROOT / "strings.json"] + sorted(
        (_INTEGRATION_ROOT / "translations").glob("*.json")
    )
    assert len(files) == 11, [f.name for f in files]
    problems: list[str] = []
    for path in files:
        progress = (
            json.loads(path.read_text(encoding="utf-8"))
            .get("config", {})
            .get("progress", {})
        )
        for key in _KEYS:
            text = progress.get(key)
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{path.name}: {key} missing")
                continue
            if re.search(r"\d", text):
                problems.append(f"{path.name}: {key} has a digit")
        title = progress.get(_KEYS[0])
        if isinstance(title, str) and title.count("{device}") != 1:
            problems.append(f"{path.name}: title without one {{device}}")
    assert problems == []


@pytest.mark.asyncio
async def test_notice_id_and_log_carry_no_device_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the notification id nor a log line carries the device name.

    The second switch-on runs without texts, which writes the DEBUG line.
    """
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()
    caplog.set_level(logging.DEBUG, logger="custom_components.googlefindmy")

    await _set(coord, _TRACKER, True)
    await _set(coord, _TRACKER, False)

    async def _no_texts(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(translation, "async_get_translations", _no_texts)
    await _set(coord, _TRACKER, True)

    assert len(notices.created) == 1
    assert _TRACKER_NAME not in notices.created[0]["notification_id"]
    assert all(_TRACKER_NAME not in dismissed for dismissed in notices.dismissed)
    ours = [
        r for r in caplog.records if r.name.startswith("custom_components.googlefindmy")
    ]
    assert any("no text" in r.getMessage() for r in ours), [
        r.getMessage() for r in ours
    ]
    assert [r.getMessage() for r in ours if _TRACKER_NAME in r.getMessage()] == []


@pytest.mark.asyncio
async def test_failed_text_lookup_shows_nothing_and_keeps_the_switch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing translation lookup writes a DEBUG line and shows no notice.

    The error stays inside the notice task: the switch is still stored as on.
    """
    notices = _Notices(monkeypatch)
    coord = _bare_coordinator()
    caplog.set_level(logging.DEBUG, logger="custom_components.googlefindmy")

    async def _broken_lookup(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise RuntimeError("translation cache unavailable")

    monkeypatch.setattr(translation, "async_get_translations", _broken_lookup)
    await _set(coord, _TRACKER, True)

    assert notices.created == []
    assert _TRACKER in coord.config_entry.options[_OPT_ENABLED]
    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("custom_components.googlefindmy")
    ]
    assert any(
        "translation lookup failed" in m and "translation cache unavailable" in m
        for m in messages
    ), messages


def test_readme_names_the_notice_automation() -> None:
    """README: the notice is not sent to a phone; an automation can forward it."""
    text = _README.read_text(encoding="utf-8")
    section = text.split("### Automatic location polling per tracker", 1)[-1]
    section = section.split("\n### ", 1)[0]
    assert "Turning the switch on shows a Home Assistant notification" in section
    assert "`persistent_notification`" in section
    assert "`notify.mobile_app_<device>`" in section


def test_switch_docstring_names_the_notice() -> None:
    """``switch.py`` says who creates the notice."""
    doc = switch.__doc__ or ""
    assert "notification" in doc
    assert "never the entity" in doc

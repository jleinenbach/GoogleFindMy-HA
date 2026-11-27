# tests/test_automatic_polling_notice.py
"""Notification when automatic location polling is turned on for a device.

Turning the switch *Automatic location polling* on shows one Home Assistant
notification that explains what the requests do on the device: a phone listed
without a canonical ID gets the phone text, a phone listed with a canonical ID
gets nothing, every other device gets the tracker text. Turning the switch off
and removing the device (``purge_device``) remove it, and unloading the entry
cancels one that is still loading its text. Neither a restart nor the setup of
the switch creates one: the coordinator's setter is the only place that shows
it. The static check of that sees the forms listed in ``_is_create``.

``async_create`` and ``async_dismiss`` are replaced on the module
``homeassistant.components.persistent_notification`` itself, so a call through
that module's attribute is counted from any module of the integration, not only
from the coordinator. A name bound at import time (``from ... import
async_create``) keeps the original function and is not counted here; only the
static check sees that form.

One test runs the real translation layer in this process: under
``use_real_homeassistant_modules`` it imports ``coordinator.main`` freshly,
so the module binds the real loader, translation helper and notification
store instead of the stubbed ones.

New names of the feature are looked up with ``getattr`` and a ``pytest.fail``
instead of an import, so that on the commit before the feature this module is
collected and fails test by test instead of erroring at collection.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import logging
import re
import sys
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
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
        assert category == "config"
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
        await _drain(self.tasks)


async def _drain(tasks: list[asyncio.Task[Any]]) -> None:
    """Await ``tasks``; a cancelled task is fine, any other error is raised."""
    for _ in range(20):
        if not tasks:
            return
        pending = list(tasks)
        tasks.clear()
        results = await asyncio.gather(*pending, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                raise result
    for task in tasks:
        task.cancel()
    pytest.fail("planned tasks keep planning new tasks")


def _plan_on_entry(entry: Any, tasks: list[asyncio.Task[Any]]) -> Any:
    """Give ``entry`` the core ``async_create_background_task``, kept in ``tasks``."""

    def _create(
        _hass: Any, coro: Any, name: str, eager_start: bool = True
    ) -> asyncio.Task[Any]:
        task = asyncio.Task(
            coro, loop=asyncio.get_running_loop(), name=name, eager_start=eager_start
        )
        tasks.append(task)
        return task

    entry.async_create_background_task = _create
    return entry


def _bare_coordinator(
    *, phone_route: frozenset[str] = frozenset(), rows: dict[str, Any] | None = None
) -> GoogleFindMyCoordinator:
    """The production class without ``__init__``, with the fields the notice reads."""
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord.hass = _Hass()  # type: ignore[assignment]
    coord.config_entry = _plan_on_entry(
        make_config_entry(
            entry_id=_ENTRY_ID, options={_OPT_DISABLED: [], _OPT_ENABLED: []}
        ),
        coord.hass.tasks,  # type: ignore[attr-defined]
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


@pytest.mark.asyncio
async def test_removing_device_while_text_loads_leaves_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``purge_device`` during the text lookup cancels the planned notice.

    The stored choice stays "on" (``purge_device`` does not clear it), so only
    the cancelled task keeps the notice away, not the second switch check.
    """
    notices = _Notices(monkeypatch)
    coord = _make_coordinator(monkeypatch, options={}, api=_DispatchingAPI())
    tasks: list[asyncio.Task[Any]] = []
    coord.config_entry = _plan_on_entry(
        make_config_entry(
            entry_id=_ENTRY_ID, options={_OPT_DISABLED: [], _OPT_ENABLED: []}
        ),
        tasks,
    )
    coord.hass.config_entries = _CoreLikeConfigEntries()  # type: ignore[attr-defined]
    coord.hass.config = SimpleNamespace(language="en")  # type: ignore[attr-defined]
    coord.async_update_listeners = lambda: None  # type: ignore[method-assign]
    coord.async_set_updated_data = lambda _data: None  # type: ignore[method-assign]
    coord.data = []
    _english_texts(monkeypatch, while_loading=lambda: coord.purge_device(_TRACKER))

    coord.async_set_automatic_polling(_TRACKER, True)
    assert len(tasks) == 1, "the notice task is planned on the config entry"
    await _drain(tasks)

    assert coord.config_entry.options[_OPT_ENABLED] == [_TRACKER]
    assert notices.created == []
    assert notices.dismissed == [_notice_id(_TRACKER)]


# ---------------------------------------------------------------------------
# How the notice task is planned
# ---------------------------------------------------------------------------


def _reduced_double(form: str, tasks: list[asyncio.Task[Any]]) -> Any:
    """A double of ``async_create_background_task`` in a reduced signature."""
    loop = asyncio.get_running_loop()
    if form == "no_eager_start":

        def _without_eager_start(_hass: Any, coro: Any, name: str) -> Any:
            task = loop.create_task(coro, name=name)
            tasks.append(task)
            return task

        return _without_eager_start

    def _coro_only(_hass: Any, coro: Any) -> Any:
        task = loop.create_task(coro)
        tasks.append(task)
        return task

    return _coro_only


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["no_eager_start", "coro_only"])
async def test_notice_planned_through_a_reduced_task_double(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    """A double without ``eager_start`` or with only ``(hass, coro)`` still works."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()
    coord.config_entry.async_create_background_task = _reduced_double(  # type: ignore[union-attr]
        form,
        coord.hass.tasks,  # type: ignore[attr-defined]
    )

    await _set(coord, _TRACKER, True)

    assert [n["notification_id"] for n in notices.created] == [_notice_id(_TRACKER)]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["without_helper", "none"])
async def test_no_background_task_helper_plans_nothing(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """Without ``async_create_background_task`` on the entry nothing is planned."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()
    if entry == "none":
        coord.config_entry = None  # type: ignore[assignment]
    else:
        del coord.config_entry.async_create_background_task  # type: ignore[union-attr]

    _require(coord, "_schedule_automatic_polling_notice")(_TRACKER)

    assert coord.hass.tasks == []  # type: ignore[attr-defined]
    assert coord._automatic_polling_notice_tasks() == {}
    assert notices.created == []


@pytest.mark.asyncio
async def test_double_returning_no_task_tracks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double that returns no task keeps the coroutine; nothing is tracked."""
    notices = _Notices(monkeypatch)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()
    kept: list[Any] = []

    def _keep(_hass: Any, coro: Any, name: str, eager_start: bool = True) -> None:
        kept.append(coro)

    coord.config_entry.async_create_background_task = _keep  # type: ignore[union-attr]

    coord.async_set_automatic_polling(_TRACKER, True)
    try:
        assert len(kept) == 1
        assert coord._automatic_polling_notice_tasks() == {}
    finally:
        for coro in kept:
            coro.close()
    assert notices.created == []


@pytest.mark.asyncio
async def test_failing_notice_task_logs_only_the_error_class(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash of the task writes one WARNING with the error class, not its text."""

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("private detail")

    monkeypatch.setattr(persistent_notification, "async_create", _fail)
    _english_texts(monkeypatch)
    coord = _bare_coordinator()
    caplog.set_level(logging.WARNING)

    coord.async_set_automatic_polling(_TRACKER, True)
    results = await asyncio.gather(
        *coord.hass.tasks,  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    await asyncio.sleep(0)

    assert [type(result) for result in results] == [RuntimeError]
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "Automatic polling notice" in record.getMessage()
    ]
    assert lines == [
        f"Automatic polling notice for canonical={_TRACKER} failed (RuntimeError)"
    ]
    assert "private detail" not in caplog.text
    assert coord._automatic_polling_notice_tasks() == {}


# ---------------------------------------------------------------------------
# Only the setter creates a notice
# ---------------------------------------------------------------------------


def _texts_held_until(
    monkeypatch: pytest.MonkeyPatch, release: asyncio.Event
) -> list[str]:
    """Serve the English texts once ``release`` is set; record each lookup."""
    started: list[str] = []

    async def _lookup(
        _hass: Any, language: str, category: str, integrations: Any = None
    ) -> dict[str, str]:
        started.append(language)
        await release.wait()
        return {
            f"component.{DOMAIN}.config.progress.{key}": value
            for key, value in _EN.items()
        }

    monkeypatch.setattr(translation, "async_get_translations", _lookup)
    return started


@pytest.mark.asyncio
async def test_switch_on_again_while_the_first_notice_loads_keeps_the_new_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On, off and on again within one lookup: the second task stays tracked.

    The end of the first task must not drop the second one, so removing the
    device afterwards (the dismiss that ``purge_device`` runs) still cancels it.
    The stored choice is "on", so only that cancellation keeps the notice away.
    """
    notices = _Notices(monkeypatch)
    coord = _bare_coordinator()
    release = asyncio.Event()
    started = _texts_held_until(monkeypatch, release)
    setter = _require(coord, "async_set_automatic_polling")
    pending = _require(coord, "_automatic_polling_notice_tasks")
    tasks = coord.hass.tasks  # type: ignore[attr-defined]

    setter(_TRACKER, True)
    await asyncio.sleep(0)
    assert started == ["en"], "the first task is loading its texts"
    setter(_TRACKER, False)
    setter(_TRACKER, True)
    first, second = tasks
    for _ in range(3):
        await asyncio.sleep(0)
    assert first.cancelled()
    assert started == ["en", "en"], "the second task is loading its texts"
    assert pending() == {_TRACKER: second}

    _require(coord, "_dismiss_automatic_polling_notice")(_TRACKER)
    release.set()
    await _drain(tasks)

    assert second.cancelled()
    assert coord.config_entry.options[_OPT_ENABLED] == [_TRACKER]
    assert notices.created == []
    assert pending() == {}


@pytest.mark.asyncio
async def test_cancelled_notice_task_ends_without_an_error_report(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A notice cancelled while it loads ends quietly: no loop error, no warning.

    The done callback must not ask a cancelled task for its exception: that
    raises inside the callback, and asyncio hands it to the loop's exception
    handler.
    """
    notices = _Notices(monkeypatch)
    coord = _bare_coordinator()
    release = asyncio.Event()
    started = _texts_held_until(monkeypatch, release)
    setter = _require(coord, "async_set_automatic_polling")
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        with caplog.at_level(logging.WARNING):
            setter(_TRACKER, True)
            (task,) = coord.hass.tasks  # type: ignore[attr-defined]
            await asyncio.sleep(0)
            assert started == ["en"], "the task is loading its texts"
            setter(_TRACKER, False)
            await _drain(coord.hass.tasks)  # type: ignore[attr-defined]
            for _ in range(3):
                await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert task.cancelled()
    assert reported == []
    assert [
        record
        for record in caplog.records
        if record.name.startswith("custom_components.googlefindmy")
    ] == []
    assert notices.created == []


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
    coord.config_entry = _plan_on_entry(entry, hass.tasks)
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


_CREATE_NAMES = frozenset({"async_create", "create"})


def _notification_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to the core module and to its ``async_create``/``create``."""
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
                    and alias.name in _CREATE_NAMES
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


def _is_create(call: ast.Call, modules: set[str], functions: set[str]) -> bool:
    """Whether ``call`` creates a core notification, in one of four forms.

    Seen: ``persistent_notification.async_create``/``create``, an imported
    ``async_create``/``create``, ``getattr(persistent_notification, "...create")``
    with a literal name, and the service call ``persistent_notification.create``
    with domain and service as positional string literals.

    Not seen, so a hit there is not counted:

    * an alias by assignment (``f = pn.async_create; f(...)``), also as an
      assignment expression (``(f := pn.async_create)(...)``);
    * a call through ``__call__`` (``pn.async_create.__call__(...)``);
    * the function handed over instead of called: ``hass.add_job(pn.create, ...)``,
      ``hass.async_add_executor_job(pn.create, ...)``, ``functools.partial``;
    * the service call with ``domain=``/``service=`` as keywords, or with the
      domain as ``pn.DOMAIN`` or a variable, or with the service name as a
      variable;
    * ``homeassistant.components.persistent_notification.async_create`` reached
      through ``import homeassistant.components`` or
      ``from homeassistant import components``;
    * ``importlib.import_module(...)`` and ``getattr`` with a computed name;
    * a write into the notification store itself.

    None of these forms occurs in the integration today; a change that adds one
    needs this detector extended first.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in _CREATE_NAMES and _dotted(func.value) in modules:
            return True
        if func.attr in ("async_call", "call") and len(call.args) >= 2:
            domain, service = call.args[0], call.args[1]
            return (
                isinstance(domain, ast.Constant)
                and domain.value == "persistent_notification"
                and isinstance(service, ast.Constant)
                and service.value == "create"
            )
        return False
    if isinstance(func, ast.Name):
        if func.id in functions:
            return True
        if func.id == "getattr" and len(call.args) >= 2:
            owner, attr = call.args[0], call.args[1]
            return (
                _dotted(owner) in modules
                and isinstance(attr, ast.Constant)
                and attr.value in _CREATE_NAMES
            )
    return False


def _creates_in(source: str) -> int:
    tree = ast.parse(source)
    modules, functions = _notification_aliases(tree)
    return sum(
        _is_create(call, modules, functions) for call, _ in _calls_with_function(tree)
    )


@pytest.mark.parametrize(
    "source",
    [
        "from homeassistant.components import persistent_notification as pn\n"
        "pn.async_create(hass, 'm')",
        "from homeassistant.components import persistent_notification\n"
        "persistent_notification.create(hass, 'm')",
        "from homeassistant.components.persistent_notification import create as c\n"
        "c(hass, 'm')",
        "from homeassistant.components import persistent_notification\n"
        "getattr(persistent_notification, 'async_create')(hass, 'm')",
        "hass.services.async_call('persistent_notification', 'create', {})",
        "hass.services.call('persistent_notification', 'create', {})",
    ],
)
def test_create_detector_sees_every_form(source: str) -> None:
    """The detector below counts each way to a core notification once."""
    assert _creates_in(source) == 1


def test_notice_created_only_from_the_switch_setter() -> None:
    """One way to a notice: switch entity -> setter -> schedule -> show -> create."""
    schedules: list[tuple[str, str]] = []
    shows: list[tuple[str, str]] = []
    creates: list[tuple[str, str]] = []
    setters: list[tuple[str, str]] = []
    for relative, tree in _package_trees():
        modules, functions = _notification_aliases(tree)
        for call, owner in _calls_with_function(tree):
            func = call.func
            if _is_create(call, modules, functions):
                creates.append((relative, owner))
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr == "_schedule_automatic_polling_notice":
                schedules.append((relative, owner))
            if func.attr == "_async_show_automatic_polling_notice":
                shows.append((relative, owner))
            if func.attr == "async_set_automatic_polling":
                setters.append((relative, owner))

    assert schedules == [("coordinator/main.py", "async_set_automatic_polling")], (
        schedules
    )
    assert shows == [("coordinator/main.py", "_schedule_automatic_polling_notice")], (
        shows
    )
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


@pytest.mark.asyncio
async def test_choice_stored_off_elsewhere_while_text_loads_leaves_no_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch is read again after the text loads, not only cancelled.

    The stored choice turns "off" without the setter (an options update of the
    entry), so no task is cancelled; only the second read keeps the notice away.
    """
    notices = _Notices(monkeypatch)
    coord = _bare_coordinator()

    def _store_off() -> None:
        coord.hass.config_entries.async_update_entry(  # type: ignore[attr-defined]
            coord.config_entry,
            options={_OPT_DISABLED: [_TRACKER], _OPT_ENABLED: []},
        )

    _english_texts(monkeypatch, while_loading=_store_off)

    await _set(coord, _TRACKER, True)

    assert coord.config_entry.options[_OPT_DISABLED] == [_TRACKER]
    assert notices.dismissed == []
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
        "translation lookup failed" in m and "RuntimeError" in m for m in messages
    ), messages
    assert [m for m in messages if "translation cache unavailable" in m] == []


# ---------------------------------------------------------------------------
# The real translation layer, without any test double
# ---------------------------------------------------------------------------

_PACKAGE = "custom_components.googlefindmy"


def _fresh_import(
    monkeypatch: pytest.MonkeyPatch, name: str, prefix: str
) -> ModuleType:
    """Import ``name`` freshly and hand every new ``prefix*`` key to monkeypatch.

    Same helper as in ``tests/test_services_permissions.py``: a key the import
    creates is popped and set again through ``monkeypatch``, so ``monkeypatch``
    removes it on teardown; a key that existed before gets its old value back.
    """

    def _keys() -> set[str]:
        return {k for k in sys.modules if k == prefix or k.startswith(f"{prefix}.")}

    before = _keys()
    monkeypatch.delitem(sys.modules, name, raising=False)
    parent_name, _, child = name.rpartition(".")
    parent = importlib.import_module(parent_name)
    monkeypatch.setattr(parent, child, getattr(parent, child, None), raising=False)
    module = importlib.import_module(name)
    for key in sorted(_keys() - before):
        created = sys.modules.pop(key)
        monkeypatch.setitem(sys.modules, key, created)
    return module


@pytest.fixture
def real_main(
    use_real_homeassistant_modules: None, monkeypatch: pytest.MonkeyPatch
) -> ModuleType:
    """``coordinator.main`` imported freshly against the real Home Assistant modules.

    The module imported at collection stays bound to the stubbed modules
    (``tests/AGENTS.md``, fresh import under real Home Assistant modules), so it
    would not run the real loader and translation helper. The fresh copy binds
    the real ``translation`` and ``persistent_notification``.
    """
    return _fresh_import(monkeypatch, f"{_PACKAGE}.coordinator.main", _PACKAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "de"])
async def test_notice_through_the_real_translation_layer(
    real_main: ModuleType, language: str
) -> None:
    """The shipped texts reach a real notification, in English and German.

    Home Assistant, its loader, the translation helper and the notification
    store are the real ones, and none of them is replaced; only the config
    entry, the API and the device name are plain stand-ins. The category, the
    key path and the files are the ones Home Assistant itself uses, so a wrong
    category shows no notice here.
    """
    from homeassistant import loader  # noqa: PLC0415 - the real module, after the swap
    from homeassistant.core import HomeAssistant  # noqa: PLC0415

    root = _INTEGRATION_ROOT.parents[1]
    hass = HomeAssistant(str(root))
    hass.config.skip_pip = True
    hass.config.language = language
    loader.async_setup(hass)
    try:
        coord = real_main.GoogleFindMyCoordinator.__new__(
            real_main.GoogleFindMyCoordinator
        )
        coord.hass = hass
        coord.config_entry = make_config_entry(
            entry_id="entry-real",
            options={_OPT_ENABLED: [_TRACKER], _OPT_DISABLED: []},
        )
        coord.api = SimpleNamespace(has_phone_target=lambda _dev: False)
        coord._device_location_data = {}
        coord.get_device_display_name = lambda _dev: _TRACKER_NAME
        await coord._async_show_automatic_polling_notice(_TRACKER)
        store = real_main.persistent_notification._async_get_or_create_notifications(
            hass
        )
        shown = [[n["title"], n["message"]] for n in store.values()]
    finally:
        await hass.async_stop(force=True)

    texts = json.loads(
        (_INTEGRATION_ROOT / "translations" / f"{language}.json").read_text(
            encoding="utf-8"
        )
    )["config"]["progress"]
    assert shown == [
        [
            texts["automatic_polling_notice_title"].replace("{device}", _TRACKER_NAME),
            texts["automatic_polling_notice_tracker"],
        ]
    ]


@pytest.mark.asyncio
async def test_unloading_the_entry_cancels_a_notice_still_loading(
    real_main: ModuleType,
) -> None:
    """The task is a background task of a real config entry; unloading cancels it.

    ``_async_process_on_unload`` is what Home Assistant runs when it unloads an
    entry: it cancels the entry's background tasks and only waits for the
    others, so a normal entry task would still show the notice.
    """
    from homeassistant import loader  # noqa: PLC0415 - the real module, after the swap
    from homeassistant.config_entries import ConfigEntry  # noqa: PLC0415
    from homeassistant.core import HomeAssistant  # noqa: PLC0415

    hass = HomeAssistant(str(_INTEGRATION_ROOT.parents[1]))
    hass.config.skip_pip = True
    hass.config.language = "en"
    loader.async_setup(hass)
    try:
        entry = ConfigEntry(
            data={},
            discovery_keys=MappingProxyType({}),
            domain=DOMAIN,
            entry_id=_ENTRY_ID,
            minor_version=1,
            options={_OPT_ENABLED: [_TRACKER], _OPT_DISABLED: []},
            source="user",
            subentries_data=None,
            title="Test Account",
            unique_id=None,
            version=1,
        )
        coord = real_main.GoogleFindMyCoordinator.__new__(
            real_main.GoogleFindMyCoordinator
        )
        coord.hass = hass
        coord.config_entry = entry
        coord.api = SimpleNamespace(has_phone_target=lambda _dev: False)
        coord._device_location_data = {}
        coord.get_device_display_name = lambda _dev: _TRACKER_NAME

        coord._schedule_automatic_polling_notice(_TRACKER)
        assert not entry._tasks, "planned as a normal entry task"
        (task,) = entry._background_tasks
        await asyncio.sleep(0)  # one loop step; that loading began is not checked
        await entry._async_process_on_unload(hass)

        assert task.cancelled()
        store = real_main.persistent_notification._async_get_or_create_notifications(
            hass
        )
        assert dict(store) == {}
        assert coord._automatic_polling_notice_tasks() == {}
    finally:
        await hass.async_stop(force=True)


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

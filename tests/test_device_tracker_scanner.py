# tests/test_device_tracker_scanner.py
"""Tests for the silent tracker scanner and its registry self-heal path.

A newly available tracker is an entity, not an account: it must appear without
a discovery card, without a dialog and without a user click. These tests pin
both halves of that -- the absence of any discovery flow, and the narrow
exception in which the platform still asks for one single entry reload because
the entities it created never reached the entity registry.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry

_ENTRY_ID = "entry-123"


def _device_tracker_module() -> Any:
    return importlib.import_module("custom_components.googlefindmy.device_tracker")


class _ProbeTimer:
    """Stands in for the grace timer so a test can end it on purpose.

    The platform must not judge a fresh addition before the core had a chance
    to register it, and no callback announces that moment. It therefore waits
    out a wall-clock grace period. Capturing that timer here keeps the tests
    deterministic and, more importantly, keeps them honest: firing it is an
    explicit act, so a probe that runs at any other moment shows up as a
    surprise instead of hiding in a passing assertion.
    """

    def __init__(self) -> None:
        self.armed: list[tuple[float, Callable[[Any], None]]] = []
        self.cancelled = 0

    def install(self, monkeypatch: pytest.MonkeyPatch, device_tracker: Any) -> None:
        def _fake_call_later(
            hass: Any, delay: float, action: Callable[[Any], None]
        ) -> Callable[[], None]:
            del hass
            self.armed.append((delay, action))

            def _cancel() -> None:
                self.cancelled += 1

            return _cancel

        monkeypatch.setattr(device_tracker, "async_call_later", _fake_call_later)

    @property
    def live(self) -> int:
        """Number of armed timers that were not cancelled again."""

        return len(self.armed) - self.cancelled

    def fire(self) -> None:
        """Run the most recently armed timer, as the event loop would."""

        assert self.armed, "no registry probe was armed"
        _delay, action = self.armed[-1]
        action(None)


@pytest.fixture(autouse=True)
def probe_timer(monkeypatch: pytest.MonkeyPatch) -> _ProbeTimer:
    """Replace the platform's grace timer in every test of this module."""

    timer = _ProbeTimer()
    timer.install(monkeypatch, _device_tracker_module())
    return timer


class _HassStub:
    """Minimal hass surface used by the device_tracker platform."""

    def __init__(self) -> None:
        self.data: dict[Any, Any] = {}
        self.reloaded: list[str] = []
        self.created_tasks: list[Any] = []
        self.config_entries: Any = SimpleNamespace(
            async_schedule_reload=self.reloaded.append
        )

    def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
        """Record and close background work instead of running it.

        The platform must not schedule background work for a new tracker any
        more; recording it here makes a regression visible instead of leaking a
        never-awaited coroutine warning.
        """

        self.created_tasks.append(coro)
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return None


def _make_coordinator(
    device_tracker: Any,
    hass: _HassStub,
    devices: Iterable[Mapping[str, Any]],
    *,
    registry_hit: bool,
    registry_hits: set[str] | None = None,
    reindex_mode: str = "ok",
) -> Any:
    """Build a coordinator stub whose registry probe answers deterministically.

    ``registry_hits`` answers per device id instead of for all of them, which is
    what a mixed outcome (some trackers registered, some not) needs.
    ``reindex_mode`` picks how the poll-target reindex behaves: ``ok`` records
    the call, ``raises`` fails inside it, ``missing`` removes the helper from the
    coordinator surface altogether (an older coordinator).
    """

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self) -> None:
            self._devices = [dict(device) for device in devices]
            self.listeners: list[Callable[[], None]] = []
            self.hass = hass
            self.config_entry = None
            self._bootstrap_consumed = False
            self._device_names: dict[str, str] = {}
            self._device_location_data: dict[str, Any] = {}
            self._device_caps: dict[str, Any] = {}
            self._present_last_seen: dict[str, float] = {}
            self.registry_hit = registry_hit
            self.registry_lookups: list[str] = []
            self.reindex_calls = 0

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            self.listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self,
            *,
            key: str | None = None,
            feature: str | None = None,
        ) -> str:
            assert key is not None
            return f"{key}-identifier"

        def get_subentry_snapshot(
            self,
            key: str | None = None,
            *,
            feature: str | None = None,
        ) -> list[dict[str, Any]]:
            if not self._bootstrap_consumed:
                self._bootstrap_consumed = True
                return []
            return [dict(device) for device in self._devices]

        def get_subentry_metadata(
            self,
            *,
            key: str | None = None,
            feature: str | None = None,
        ) -> Any:
            if key is not None:
                resolved = key
            elif feature == "binary_sensor":
                resolved = SERVICE_SUBENTRY_KEY
            else:
                resolved = TRACKER_SUBENTRY_KEY
            return SimpleNamespace(key=resolved)

        def find_tracker_entity_entry(self, device_id: str) -> Any:
            self.registry_lookups.append(device_id)
            if registry_hits is not None:
                if device_id not in registry_hits:
                    return None
            elif not self.registry_hit:
                return None
            return SimpleNamespace(entity_id="device_tracker.stub")

        def reindex_poll_targets(self) -> None:
            self.reindex_calls += 1
            if reindex_mode == "raises":
                raise RuntimeError("registry unavailable")

    if reindex_mode == "missing":
        # An older coordinator simply does not carry the helper; ``getattr``
        # then hands back ``None`` and the platform has to cope.
        _StubCoordinator.reindex_poll_targets = None  # type: ignore[assignment]

    return _StubCoordinator()


def _make_entry(coordinator: Any) -> SimpleNamespace:
    entry = make_config_entry(
        entry_id=_ENTRY_ID,
        data={
            CONF_GOOGLE_EMAIL: "Owner@Example.Com",
            CONF_OAUTH_TOKEN: "aas_et/ACCOUNT",
            DATA_SECRET_BUNDLE: {"Email": "Owner@Example.Com"},
        },
        runtime_data=coordinator,
    )
    entry.unload_callbacks = []
    entry.async_on_unload = entry.unload_callbacks.append
    return entry


async def _set_up_platform(
    device_tracker: Any,
    hass: _HassStub,
    *,
    registry_hit: bool,
    added: list[list[Any]],
    devices: Iterable[Mapping[str, Any]] | None = None,
    registry_hits: set[str] | None = None,
    reindex_mode: str = "ok",
) -> tuple[Any, SimpleNamespace]:
    """Run one full platform setup and return its coordinator and entry."""

    coordinator = _make_coordinator(
        device_tracker,
        hass,
        devices if devices is not None else [{"id": "tracker-1", "name": "Tracker"}],
        registry_hit=registry_hit,
        registry_hits=registry_hits,
        reindex_mode=reindex_mode,
    )
    entry = _make_entry(coordinator)
    coordinator.config_entry = entry

    def _capture_entities(
        entities: Iterable[Any], update_before_add: bool = False
    ) -> None:
        added.append(list(entities))

    await device_tracker.async_setup_entry(hass, entry, _capture_entities)
    return coordinator, entry


@pytest.mark.asyncio
async def test_a_new_tracker_becomes_an_entity_without_any_discovery_flow(
    monkeypatch: pytest.MonkeyPatch,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A newly available tracker is added silently: entities yes, discovery no.

    The assertion runs through the real listener, not over the module source:
    the trigger is spied on in ``discovery`` (its remaining home, used by the
    secrets file watcher), so re-adding a call from the platform under any
    alias would still be caught.
    """

    del deterministic_config_subentry_id  # fixture patches ensure_config_subentry_id

    device_tracker = _device_tracker_module()
    discovery = importlib.import_module("custom_components.googlefindmy.discovery")

    triggered: list[Mapping[str, Any]] = []

    async def _spy_trigger(*args: Any, **kwargs: Any) -> Any:
        triggered.append(kwargs)
        return None

    monkeypatch.setattr(discovery, "_trigger_cloud_discovery", _spy_trigger)

    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=True, added=added
    )

    # The entities are created: main tracker plus last-location tracker.
    assert added and len(added[0]) == 2
    identifier = coordinator.stable_subentry_identifier(key=TRACKER_SUBENTRY_KEY)
    tracker_entity, last_location_entity = added[0]
    assert tracker_entity.subentry_key == TRACKER_SUBENTRY_KEY
    assert identifier in tracker_entity.unique_id
    assert not tracker_entity.unique_id.endswith(":last_location")
    assert last_location_entity.unique_id.endswith(":last_location")

    # ... and nothing at all is asked of the user.
    assert triggered == []
    assert hass.created_tasks == []
    assert hass.reloaded == []
    assert entry.unload_callbacks


@pytest.mark.asyncio
async def test_the_run_that_adds_a_tracker_never_schedules_a_reload(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The registry probe must not run in the callback that adds the entities.

    Home Assistant schedules the actual entity addition as its own task, so a
    probe in the same callback is negative for *every* new tracker. Evaluating
    it there would replace the discovery card with a reload on each new device,
    the exact opposite of what this change is for.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    assert added and len(added[0]) == 2
    assert hass.reloaded == []
    # Not even asked: the ids are only remembered for a later run.
    assert coordinator.registry_lookups == []


@pytest.mark.asyncio
async def test_an_account_without_trackers_still_completes_the_platform_setup(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """An empty snapshot adds nothing, and adds it explicitly.

    Home Assistant treats a platform that never calls ``async_add_entities`` as
    an unfinished setup, and an unfinished setup makes the later unload fail. So
    the empty case is not a silent return: it schedules an empty add. It must
    also stay silent in every other respect, because "no devices" is a normal
    state for a fresh account, not a defect to be healed by a reload.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []

    coordinator = _make_coordinator(device_tracker, hass, [], registry_hit=False)
    entry = _make_entry(coordinator)
    coordinator.config_entry = entry

    def _capture(entities: Iterable[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))

    await device_tracker.async_setup_entry(hass, entry, _capture)

    # Measured, not assumed: the bootstrap pass and the listener pass each add
    # once, and both add nothing. The count is incidental, the property is not.
    assert added, "an omitted add leaves the platform unfinished and breaks unload"
    assert all(batch == [] for batch in added)
    assert hass.reloaded == []
    assert coordinator.registry_lookups == []


@pytest.mark.asyncio
async def test_a_later_listener_run_is_not_treated_as_a_barrier(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """No number of listener runs may stand in for the grace period.

    A poll cycle publishes a per-device push and its closing snapshot back to
    back, and neither notification waits for the add task the core scheduled.
    Judging the addition on "the next listener run" therefore reloads an entry
    whose entities were perfectly on their way (Codex, PR #1222).
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    # Two further notifications in immediate succession, exactly as a poll
    # cycle produces them.
    coordinator.listeners[0]()
    coordinator.listeners[0]()

    assert coordinator.registry_lookups == []
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_the_probe_reloads_once_when_the_entities_never_registered(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The self-heal path fires once the grace period is over, and only then."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    assert hass.reloaded == []
    assert probe_timer.armed and probe_timer.armed[-1][0] == pytest.approx(
        device_tracker._REGISTRY_PROBE_DELAY
    )

    probe_timer.fire()

    assert _coordinator.registry_lookups == ["tracker-1"]
    assert hass.reloaded == [entry.entry_id]


@pytest.mark.asyncio
async def test_no_reload_when_the_registry_confirms_the_trackers(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The ordinary case stays silent: confirmed entities, no reload."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=True, added=added
    )

    probe_timer.fire()

    assert coordinator.registry_lookups == ["tracker-1"]
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_a_second_probe_run_judges_nothing_a_second_time(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The probe judges each addition once, and only once.

    It consumes the pending ids when it runs. A further run with nothing pending
    must be a no-op: asking the registry again would cost a lookup per device for
    an answer nobody waits for, and treating "nothing pending" as "nothing found"
    would reload a perfectly healthy entry.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=True, added=added
    )

    probe_timer.fire()
    assert coordinator.registry_lookups == ["tracker-1"]

    probe_timer.fire()

    assert coordinator.registry_lookups == ["tracker-1"], (
        "the second run must not ask the registry again"
    )
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_the_grace_period_is_cancelled_when_the_entry_unloads(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """An armed timer must not outlive the platform that armed it."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    assert probe_timer.live == 1

    for unload_callback in entry.unload_callbacks:
        unload_callback()

    assert probe_timer.live == 0
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_an_awaitable_timer_stub_is_scheduled_instead_of_kept(
    monkeypatch: pytest.MonkeyPatch,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A stub returning an awaitable must not be stored as an unsub handle.

    Mirrors the guard the integration already uses elsewhere for
    ``async_call_later``: awaiting is scheduled, and unloading stays harmless
    because there is nothing to cancel.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()

    async def _later_coro() -> None:
        return None

    def _awaitable_call_later(hass: Any, delay: float, action: Any) -> Any:
        del hass, delay, action
        return _later_coro()

    monkeypatch.setattr(device_tracker, "async_call_later", _awaitable_call_later)

    hass = _HassStub()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    assert len(hass.created_tasks) == 1
    for unload_callback in entry.unload_callbacks:
        unload_callback()
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_a_further_addition_restarts_the_grace_period(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A tracker added later gets the full grace period, not the remainder."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    assert len(probe_timer.armed) == 1

    coordinator._devices.append({"id": "tracker-2", "name": "Second"})
    coordinator.listeners[0]()

    assert len(probe_timer.armed) == 2
    assert probe_timer.cancelled == 1, "the first timer has to be dropped"
    assert probe_timer.live == 1


@pytest.mark.asyncio
async def test_the_selfheal_reload_does_not_repeat_across_the_reload_it_causes(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The one-shot latch has to survive the reload it schedules.

    A reload tears the device_tracker platform down and ``async_unload_entry``
    pops the entry's bucket under ``hass.data[DOMAIN]["entries"]``. A marker in
    either place would be fresh again by the time the rebuilt platform probes,
    and a permanently empty registry would reload in a loop. This test walks
    that exact transition.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []

    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    probe_timer.fire()
    assert hass.reloaded == [entry.entry_id]

    # Reload: the entry bucket is dropped and the platform is set up afresh,
    # so every piece of platform-local state starts over.
    entries_bucket = hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})
    entries_bucket[entry.entry_id] = object()
    entries_bucket.pop(entry.entry_id, None)

    _coordinator_after, entry_after = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    assert entry_after.entry_id == entry.entry_id
    probe_timer.fire()

    assert hass.reloaded == [entry.entry_id], "a second reload would be a loop"


@pytest.mark.asyncio
async def test_the_latch_is_not_burned_when_the_core_cannot_reload(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """An older core without ``async_schedule_reload`` keeps the attempt open.

    Claiming the latch before resolving the lever would consume the entry's
    only self-heal attempt without ever reloading anything.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    hass.config_entries = SimpleNamespace()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    probe_timer.fire()

    assert hass.reloaded == []
    claimed = hass.data.get(DOMAIN, {}).get("registry_selfheal_reloads", set())
    assert entry.entry_id not in claimed


@pytest.mark.asyncio
async def test_a_missing_registry_helper_neither_reloads_nor_burns_the_latch(
    caplog: pytest.LogCaptureFixture,
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Without a registry helper the platform cannot judge, so it does nothing."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    coordinator.find_tracker_entity_entry = None
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.device_tracker")

    probe_timer.fire()

    assert hass.reloaded == []
    claimed = hass.data.get(DOMAIN, {}).get("registry_selfheal_reloads", set())
    assert entry.entry_id not in claimed
    assert any(
        "registry helper unavailable" in record.getMessage()
        for record in caplog.records
    )


def test_the_selfheal_helpers_ignore_an_empty_entry_id() -> None:
    """An entry without an id must not be claimable, and not clearable either.

    A falsy id would otherwise land in the latch set as ``""`` and swallow the
    self-heal attempt of the next entry that reads it back.
    """

    integration = importlib.import_module("custom_components.googlefindmy")
    hass = _HassStub()

    assert integration.claim_registry_selfheal_reload(hass, "") is False
    integration.discard_registry_selfheal_reload(hass, "")
    assert hass.data.get(DOMAIN, {}).get("registry_selfheal_reloads", set()) == set()


@pytest.mark.asyncio
async def test_a_failing_registry_probe_counts_as_missing(
    caplog: pytest.LogCaptureFixture,
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A raising lookup must not silently pass the tracker off as registered."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    def _boom(device_id: str) -> Any:
        raise RuntimeError("registry unavailable")

    coordinator.find_tracker_entity_entry = _boom  # type: ignore[method-assign]
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.device_tracker")

    probe_timer.fire()

    assert hass.reloaded == [entry.entry_id]
    assert any(
        "Registry lookup failed for tracker tracker-1" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_the_selfheal_reload_claims_the_shared_reload_latch(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Scheduling here has to make the other owners stand down.

    Without the claim a credential write that happens right after this reload
    would schedule a second one, and ``async_schedule_reload`` does not
    coalesce: the entry would be torn down and set up twice in a row.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    integration = importlib.import_module("custom_components.googlefindmy")
    hass = _HassStub()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    probe_timer.fire()

    assert hass.reloaded == [entry.entry_id]
    pending = hass.data.get(DOMAIN, {}).get("pending_entry_reloads", set())
    assert entry.entry_id in pending
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is False


@pytest.mark.asyncio
async def test_the_probe_stands_down_when_another_reload_is_already_on_its_way(
    caplog: pytest.LogCaptureFixture,
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A foreign reload rebuilds this platform, so a second one is waste.

    The one-shot latch has to survive that stand-down: it is spent on a reload
    this path caused, never on someone else's.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    integration = importlib.import_module("custom_components.googlefindmy")
    hass = _HassStub()
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )

    # A credential write got there first and holds the shared latch.
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is True
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.device_tracker")

    probe_timer.fire()

    assert hass.reloaded == []
    assert any(
        "a reload of this entry is already on its way" in record.getMessage()
        for record in caplog.records
    )
    claimed = hass.data.get(DOMAIN, {}).get("registry_selfheal_reloads", set())
    assert entry.entry_id not in claimed, "the one-shot attempt must survive"


@pytest.mark.asyncio
async def test_a_failed_schedule_gives_both_latches_back(
    caplog: pytest.LogCaptureFixture,
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A claim is a promise to reload; a broken promise releases both latches.

    A shared latch kept here would swallow every later reload of the entry, and
    a kept one-shot would leave the unregistered trackers without a repair.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()

    def _boom(entry_id: str) -> None:
        raise RuntimeError("event loop is gone")

    hass.config_entries = SimpleNamespace(async_schedule_reload=_boom)
    added: list[list[Any]] = []
    _coordinator, entry = await _set_up_platform(
        device_tracker, hass, registry_hit=False, added=added
    )
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.device_tracker")

    probe_timer.fire()

    domain_data = hass.data.get(DOMAIN, {})
    assert entry.entry_id not in domain_data.get("pending_entry_reloads", set())
    assert entry.entry_id not in domain_data.get("registry_selfheal_reloads", set())
    assert any(
        "Failed to schedule the self-heal reload" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_the_probe_lets_a_registered_tracker_into_the_polling_set(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A silently added tracker has to become pollable, not just visible.

    The enabled-for-polling set is derived from the entity registry but rebuilt
    only on device registry events, and the new tracker's device entry exists
    before its entity does. Without this re-derivation the tracker would carry
    an entity that never receives a position.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, _entry = await _set_up_platform(
        device_tracker, hass, registry_hit=True, added=added
    )
    assert coordinator.reindex_calls == 0

    probe_timer.fire()

    assert coordinator.reindex_calls == 1
    assert hass.reloaded == []


@pytest.mark.asyncio
async def test_the_probe_promotes_what_registered_even_while_repairing_the_rest(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A pending repair must not hold back the trackers that did land.

    The self-heal reload can still stand down (no lever, or another owner holds
    the shared latch), so the registered trackers cannot be made to wait for it.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker,
        hass,
        registry_hit=False,
        added=added,
        devices=[
            {"id": "tracker-1", "name": "Tracker One"},
            {"id": "tracker-2", "name": "Tracker Two"},
        ],
        registry_hits={"tracker-1"},
    )

    probe_timer.fire()

    assert coordinator.reindex_calls == 1
    assert hass.reloaded == [entry.entry_id]


@pytest.mark.asyncio
async def test_a_coordinator_without_the_reindex_helper_still_repairs(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing helper is logged and skipped, it never eats the self-heal."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker,
        hass,
        registry_hit=False,
        added=added,
        devices=[
            {"id": "tracker-1", "name": "Tracker One"},
            {"id": "tracker-2", "name": "Tracker Two"},
        ],
        registry_hits={"tracker-1"},
        reindex_mode="missing",
    )

    with caplog.at_level(logging.DEBUG, logger=device_tracker._LOGGER.name):
        probe_timer.fire()

    assert coordinator.reindex_calls == 0
    assert hass.reloaded == [entry.entry_id]
    assert any(
        record.levelno == logging.WARNING
        and "cannot reindex poll targets" in record.getMessage()
        for record in caplog.records
    ), "a tracker that never polls again is a warning, not a debug whisper"


@pytest.mark.asyncio
async def test_a_failing_reindex_still_repairs_the_missing_trackers(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reindex that raises must not swallow the self-heal reload behind it."""

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker,
        hass,
        registry_hit=False,
        added=added,
        devices=[
            {"id": "tracker-1", "name": "Tracker One"},
            {"id": "tracker-2", "name": "Tracker Two"},
        ],
        registry_hits={"tracker-1"},
        reindex_mode="raises",
    )

    with caplog.at_level(logging.DEBUG, logger=device_tracker._LOGGER.name):
        probe_timer.fire()

    assert coordinator.reindex_calls == 1
    assert hass.reloaded == [entry.entry_id]
    assert any(
        record.levelno == logging.WARNING
        and "reindexing poll targets after" in record.getMessage()
        and record.exc_info is not None
        for record in caplog.records
    ), "the failure has to name itself and carry its traceback"


@pytest.mark.asyncio
async def test_the_probe_promotes_nothing_when_nothing_registered(
    probe_timer: _ProbeTimer,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """No tracker reached the registry, so there is nothing to promote.

    The promotion runs independently of the missing-check, not independently of
    the count: re-deriving the polling set here would cost a full rebuild and
    could only reproduce the verdict the last device-registry event already
    reached. The self-heal reload is the answer in this case, and it still runs.
    """

    del deterministic_config_subentry_id

    device_tracker = _device_tracker_module()
    hass = _HassStub()
    added: list[list[Any]] = []
    coordinator, entry = await _set_up_platform(
        device_tracker,
        hass,
        registry_hit=False,
        added=added,
        registry_hits=set(),
    )

    probe_timer.fire()

    assert coordinator.reindex_calls == 0, (
        "with nothing registered the rebuild has no new input to work from"
    )
    assert hass.reloaded == [entry.entry_id], (
        "the missing trackers still have to trigger the one self-heal reload"
    )

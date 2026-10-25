# tests/test_fcm_entry_self_write_receipt.py
"""The update listener tells a user's credential change from the receiver's own write.

Both arrive as an ``async_update_entry`` that changes
``secrets_data["fcm_credentials"]``. A change the user made (an imported
``secrets.json``) has to become effective, which needs exactly one reload,
because the token cache is only seeded in ``async_setup_entry``. The receiver
writing back a registration it has just obtained must not reload: the live
cache already holds that value, and a reload would tear down the receiver that
produced it.

The listener is driven directly, not through a config flow: the flows schedule
their own reload, which would hide a listener that stays quiet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)
from custom_components.googlefindmy.entry_self_write import (
    SELF_WRITE_RECEIPTS_KEY,
    credential_member_digest,
    note_self_write,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_hass_data_layout import _prepare_async_setup_entry_harness


def _registration(token: str) -> dict[str, Any]:
    """Return a minimal FCM registration whose token identifies it."""

    return {
        "gcm": {"android_id": "1", "security_token": "2"},
        "fcm": {"registration": {"token": token}},
    }


async def _set_up_entry_with_bundle(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> tuple[Any, Any, Any, Callable[..., Any]]:
    """Set up an entry that keeps its bundle and return its update listener."""

    loop = asyncio.get_running_loop()
    harness = _prepare_async_setup_entry_harness(
        monkeypatch, stub_coordinator_factory, loop
    )
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass

    entry.data = {
        CONF_GOOGLE_EMAIL: "user@example.com",
        DATA_SECRET_BUNDLE: {
            "username": "user@example.com",
            "fcm_credentials": _registration("setup-registration"),
        },
    }
    # A primed cache keeps the legacy migration from moving the bundle out of
    # the entry, so the listener sees the bundle the way a modern entry has it.
    harness.cache.values = {integration.username_string: "user@example.com"}

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry) is True
    assert DATA_SECRET_BUNDLE in entry.data
    assert len(entry._update_listeners) == 1
    return integration, entry, hass, entry._update_listeners[0]


@pytest.mark.asyncio
async def test_user_import_changing_only_fcm_credentials_schedules_one_reload(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A re-imported bundle that differs only in the registration reloads once."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )

    imported_bundle = {
        **entry.data[DATA_SECRET_BUNDLE],
        "fcm_credentials": _registration("imported-registration"),
    }
    entry.data = {**entry.data, DATA_SECRET_BUNDLE: imported_bundle}
    await notify(hass, entry)
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "an imported registration has to become effective without a restart, "
        "and a second notification must not schedule a second reload"
    )


@pytest.mark.asyncio
async def test_receiver_self_write_schedules_no_reload(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The receiver writing back its own registration does not reload."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry(
        entry.entry_id, _registration("regenerated-registration")
    )

    assert len(hass.config_entries.entry_update_calls) == 1
    persisted = entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"]
    assert persisted["fcm"]["registration"]["token"] == "regenerated-registration"

    # Home Assistant notifies every update listener after an entry update.
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == []


def _receiver_for(hass: Any, entry: Any) -> FcmReceiverHA:
    """Return a receiver attached to ``hass`` that knows ``entry``'s registration."""

    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    receiver._note_entry_registration(entry)
    return receiver


def _receipts(hass: Any) -> dict[str, str]:
    """Return the receipt map the receiver writes into ``hass.data``."""

    receipts: dict[str, str] = hass.data.get(DOMAIN, {}).get(
        SELF_WRITE_RECEIPTS_KEY, {}
    )
    return receipts


@pytest.mark.asyncio
async def test_two_receiver_writes_before_the_listener_runs_schedule_no_reload(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Only the latest receipt counts, and it names the value actually stored."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)

    receiver._persist_credentials_to_entry(entry.entry_id, _registration("first"))
    receiver._persist_credentials_to_entry(entry.entry_id, _registration("second"))
    await notify(hass, entry)
    await notify(hass, entry)

    assert len(hass.config_entries.entry_update_calls) == 2
    assert hass.config_entries.scheduled_reloads == []
    assert _receipts(hass) == {}


@pytest.mark.asyncio
async def test_a_user_import_after_the_receiver_write_still_reloads_once(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The receipt names the receiver's value, not the one the user stored."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)

    receiver._persist_credentials_to_entry(entry.entry_id, _registration("renewed"))
    imported_bundle = {
        **entry.data[DATA_SECRET_BUNDLE],
        "fcm_credentials": _registration("imported-registration"),
    }
    entry.data = {**entry.data, DATA_SECRET_BUNDLE: imported_bundle}
    await notify(hass, entry)
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == [entry.entry_id]


@pytest.mark.asyncio
async def test_a_receiver_write_beside_another_change_reloads_once(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The receipt excuses the registration only, never a change beside it."""

    _integration, entry, hass, notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)

    receiver._persist_credentials_to_entry(entry.entry_id, _registration("renewed"))
    changed_bundle = {**entry.data[DATA_SECRET_BUNDLE], "shared_key": "new-shared"}
    entry.data = {**entry.data, DATA_SECRET_BUNDLE: changed_bundle}
    await notify(hass, entry)

    assert hass.config_entries.scheduled_reloads == [entry.entry_id]
    assert _receipts(hass) == {}


@pytest.mark.asyncio
async def test_a_new_setup_drops_a_receipt_of_the_previous_one(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A reload between write and listener leaves no receipt behind.

    The old listener's invocation is a bygone one and returns early, so it
    never consumes the receipt; the next setup, whose fingerprint already
    includes the written value, discards it. Removing the entry does too.
    """

    integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    receiver._persist_credentials_to_entry(entry.entry_id, _registration("renewed"))
    assert entry.entry_id in _receipts(hass)

    assert await integration.async_unload_entry(hass, entry) is True
    assert await integration.async_setup_entry(hass, entry) is True
    assert entry.entry_id not in _receipts(hass)

    receiver._persist_credentials_to_entry(entry.entry_id, _registration("again"))
    assert entry.entry_id in _receipts(hass)
    await integration.async_remove_entry(hass, entry)
    assert entry.entry_id not in _receipts(hass)


@pytest.mark.asyncio
async def test_the_receiver_leaves_a_registration_it_did_not_write_alone(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Compare-and-swap: a user import still on its way to a reload wins."""

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    imported_bundle = {
        **entry.data[DATA_SECRET_BUNDLE],
        "fcm_credentials": _registration("imported-registration"),
    }
    entry.data = {**entry.data, DATA_SECRET_BUNDLE: imported_bundle}
    writes_before = len(hass.config_entries.entry_update_calls)

    with caplog.at_level(logging.INFO):
        receiver._persist_credentials_to_entry(entry.entry_id, _registration("renewed"))

    assert len(hass.config_entries.entry_update_calls) == writes_before
    persisted = entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"]
    assert persisted == _registration("imported-registration")
    assert entry.entry_id not in _receipts(hass)
    assert any("did not write" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_registering_the_coordinator_records_the_entry_registration(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The compare-and-swap baseline comes from ``register_coordinator``."""

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    # The supervisor and cache mirroring are not under test; close each
    # coroutine instead of running it.
    monkeypatch.setattr(
        receiver, "_dispatch_to_hass_loop", lambda coro, **_kwargs: coro.close()
    )

    receiver.register_coordinator(SimpleNamespace(config_entry=entry, cache=None))
    assert receiver._entry_registration_digest[entry.entry_id] == (
        credential_member_digest(_registration("setup-registration"))
    )

    receiver._persist_credentials_to_entry(entry.entry_id, _registration("renewed"))
    assert receiver._entry_registration_digest[entry.entry_id] == (
        credential_member_digest(_registration("renewed"))
    )

    receiver.unregister_coordinator(SimpleNamespace(config_entry=entry, cache=None))
    assert entry.entry_id not in receiver._entry_registration_digest


def test_an_entry_without_a_bundle_records_an_absent_registration() -> None:
    """No bundle, or data that is not a mapping, records "no registration"."""

    receiver = FcmReceiverHA()
    absent = credential_member_digest(None)
    entry = make_config_entry(entry_id="entry", data={})
    receiver._entry_registration_digest["entry"] = "stale"
    receiver._note_entry_registration(entry)
    assert receiver._entry_registration_digest["entry"] == absent
    entry.data = [1]
    receiver._entry_registration_digest["entry"] = "stale"
    receiver._note_entry_registration(entry)
    assert receiver._entry_registration_digest["entry"] == absent


def test_a_bundle_imported_after_a_registration_without_one_wins() -> None:
    """Compare-and-swap also holds for an entry that had no bundle at setup.

    A manual-login entry gets a ``secrets.json`` with its own registration
    through a flow whose reload is still on its way. A renewal in that window
    must not overwrite the imported registration.
    """

    entry = make_config_entry(entry_id="entry-a", data={})
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], True))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    receiver._note_entry_registration(entry)
    entry.data = {DATA_SECRET_BUNDLE: {"fcm_credentials": _registration("imported")}}

    receiver._persist_credentials_to_entry("entry-a", _registration("renewed"))

    assert hass.config_entries.calls == 0
    assert entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"] == _registration(
        "imported"
    )
    assert _receipts(hass) == {}


def test_a_bundle_without_a_registration_still_receives_the_renewal() -> None:
    """An absent registration matches the recorded absent one, so it is written."""

    entry = make_config_entry(entry_id="entry-a", data={})
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], True))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    receiver._note_entry_registration(entry)
    entry.data = {DATA_SECRET_BUNDLE: {"username": "user@example.com"}}

    receiver._persist_credentials_to_entry("entry-a", _registration("renewed"))

    assert hass.config_entries.calls == 1
    assert entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"] == _registration("renewed")


@pytest.mark.asyncio
async def test_a_superseded_generation_does_not_write_the_entry(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The persist path checks the generation the update was resolved for."""

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    receiver._entry_generation[entry.entry_id] = 2
    writes_before = len(hass.config_entries.entry_update_calls)

    receiver._persist_credentials_to_entry(
        entry.entry_id, _registration("renewed"), generation=1
    )
    assert len(hass.config_entries.entry_update_calls) == writes_before

    receiver._persist_credentials_to_entry(
        entry.entry_id, _registration("renewed"), generation=2
    )
    assert len(hass.config_entries.entry_update_calls) == writes_before + 1


@pytest.mark.asyncio
async def test_the_update_callback_passes_its_generation_to_the_entry_write(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A generation that is superseded between dispatch and write is honoured."""

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    seen: list[int | None] = []

    def _record(entry_id: str, creds: Any, generation: int | None = None) -> None:
        seen.append(generation)

    monkeypatch.setattr(receiver, "_persist_credentials_to_entry", _record)
    receiver.creds[entry.entry_id] = _registration("renewed")

    await receiver._async_save_credentials_for_entry(entry.entry_id, 7)

    assert seen == [7]


@pytest.mark.asyncio
async def test_an_unchanged_registration_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Writing the value the entry already holds leaves no receipt behind."""

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    writes_before = len(hass.config_entries.entry_update_calls)

    receiver._persist_credentials_to_entry(
        entry.entry_id, _registration("setup-registration")
    )

    assert len(hass.config_entries.entry_update_calls) == writes_before
    assert entry.entry_id not in _receipts(hass)


class _Manager:
    """Entry manager whose ``async_update_entry`` result is chosen per test."""

    def __init__(self, entries: list[Any], result: Any) -> None:
        self._entries = {entry.entry_id: entry for entry in entries}
        self._result = result
        self.calls = 0

    def async_get_entry(self, entry_id: str) -> Any | None:
        return self._entries.get(entry_id)

    def async_update_entry(self, entry: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        if self._result is not False:
            entry.data = dict(kwargs["data"])
        return self._result


def _bundle_entry(entry_id: str) -> Any:
    return make_config_entry(
        entry_id=entry_id,
        data={DATA_SECRET_BUNDLE: {"fcm_credentials": _registration("old")}},
    )


def test_an_update_that_stores_nothing_drops_the_receipt() -> None:
    """``async_update_entry`` returning ``False`` runs no listener."""

    entry = _bundle_entry("entry-a")
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], False))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry("entry-a", _registration("renewed"))

    assert hass.config_entries.calls == 1
    assert _receipts(hass) == {}


def test_a_failed_update_drops_the_receipt_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store error leaves a warning without the value and no receipt."""

    entry = _bundle_entry("entry-a")
    hass = SimpleNamespace(
        data={}, config_entries=_Manager([entry], RuntimeError("token-text"))
    )
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    with caplog.at_level(logging.WARNING):
        receiver._persist_credentials_to_entry("entry-a", _registration("renewed"))

    assert _receipts(hass) == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "entry-a" in warnings[0].getMessage()
    assert "token-text" not in warnings[0].getMessage()


def test_receipts_of_two_entries_on_a_shared_receiver_stay_apart() -> None:
    """Each entry's write leaves its own receipt for its own value."""

    first, second = _bundle_entry("entry-a"), _bundle_entry("entry-b")
    hass = SimpleNamespace(data={}, config_entries=_Manager([first, second], True))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry("entry-a", _registration("a"))
    receiver._persist_credentials_to_entry("entry-b", _registration("b"))

    assert _receipts(hass) == {
        "entry-a": credential_member_digest(_registration("a")),
        "entry-b": credential_member_digest(_registration("b")),
    }


@pytest.mark.asyncio
async def test_a_teardown_after_the_callback_suppresses_the_queued_entry_write(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The callback hands its driving generation to the write it queues.

    A supervisor publishes its generation around the handshake. When a fast
    reload bumps the generation after the callback queued the write but before
    the write runs, the write belongs to a superseded supervisor and is dropped.
    """

    _integration, entry, hass, _notify = await _set_up_entry_with_bundle(
        monkeypatch, stub_coordinator_factory
    )
    receiver = _receiver_for(hass, entry)
    writes_before = len(hass.config_entries.entry_update_calls)

    token = fcm_receiver_ha._FCM_DRIVING_GENERATION.set((entry.entry_id, 0))
    try:
        receiver._on_credentials_updated_for_entry(
            entry.entry_id, _registration("renewed")
        )
    finally:
        fcm_receiver_ha._FCM_DRIVING_GENERATION.reset(token)
    # The teardown of a fast reload happens before the queued write runs.
    receiver._entry_generation[entry.entry_id] = 1
    await asyncio.gather(*list(receiver._active_tasks))

    assert len(hass.config_entries.entry_update_calls) == writes_before


def test_refreshing_only_the_top_level_copy_leaves_no_receipt() -> None:
    """The bundle already holds the value; only the stale top-level copy moves.

    The credential fingerprint does not include the top-level copy, so the
    listener sees no change and would never consume a receipt.
    """

    renewed = _registration("renewed")
    entry = make_config_entry(
        entry_id="entry-a",
        data={
            "fcm_credentials": _registration("old"),
            DATA_SECRET_BUNDLE: {"fcm_credentials": renewed},
        },
    )
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], True))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry("entry-a", renewed)

    assert hass.config_entries.calls == 1
    assert entry.data["fcm_credentials"] == renewed
    assert _receipts(hass) == {}


class _RecordingCache:
    """Token cache double that records ``set`` calls."""

    def __init__(self) -> None:
        self.sets: list[tuple[str, Any]] = []

    async def set(self, name: str, value: Any) -> None:
        self.sets.append((name, value))


class _LookupFails:
    """Entry manager whose lookup raises, as a broken core state would."""

    calls = 0

    def async_get_entry(self, entry_id: str) -> Any:
        raise RuntimeError("lookup-text")

    def async_update_entry(self, entry: Any, **kwargs: Any) -> Any:
        raise AssertionError("not reached")


@pytest.mark.asyncio
async def test_a_failing_entry_write_does_not_cost_the_cache_write(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entry write is best-effort; the cache write that predates it still runs."""

    hass = SimpleNamespace(data={}, config_entries=_LookupFails())
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    receiver.creds["entry-a"] = _registration("renewed")
    cache = _RecordingCache()
    receiver._entry_caches["entry-a"] = cache

    with caplog.at_level(logging.WARNING):
        await receiver._async_save_credentials_for_entry("entry-a")

    assert cache.sets == [("fcm_credentials", _registration("renewed"))]
    assert _receipts(hass) == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "entry-a" in warnings[0].getMessage()
    assert "lookup-text" not in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_an_invalidation_reaches_the_entry_bundle() -> None:
    """An entry with a bundle re-seeds the cache from it; the invalidation goes there too."""

    entry = make_config_entry(
        entry_id="entry-a",
        data={DATA_SECRET_BUNDLE: {"fcm_credentials": _registration("old")}},
    )
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], True))
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)
    receiver._note_entry_registration(entry)
    receiver.creds["entry-a"] = _registration("old")
    cache = _RecordingCache()
    receiver._entry_caches["entry-a"] = cache

    await receiver._invalidate_fcm_tokens("entry-a")

    stored = entry.data[DATA_SECRET_BUNDLE]["fcm_credentials"]
    assert "fcm" not in stored
    assert stored == receiver.creds["entry-a"]
    assert stored is not receiver.creds["entry-a"]
    assert _receipts(hass) == {"entry-a": credential_member_digest(stored)}
    assert cache.sets and cache.sets[-1][0] == "fcm_credentials"


_EARLIER = "digest-of-an-earlier-write"


def test_a_failing_lookup_keeps_the_receipt_of_an_earlier_write() -> None:
    """The exception comes before this call leaves a receipt; the one there stays.

    An earlier write's receipt still waits for its listener; dropping it would
    make that listener reload for the receiver's own write.
    """

    hass = SimpleNamespace(data={}, config_entries=_LookupFails())
    note_self_write(hass, "entry-a", _EARLIER)
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry_best_effort(
        "entry-a", _registration("renewed")
    )

    assert _receipts(hass) == {"entry-a": _EARLIER}


def _top_level_only_entry() -> tuple[Any, dict[str, Any]]:
    renewed = _registration("renewed")
    entry = make_config_entry(
        entry_id="entry-a",
        data={
            "fcm_credentials": _registration("old"),
            DATA_SECRET_BUNDLE: {"fcm_credentials": renewed},
        },
    )
    return entry, renewed


@pytest.mark.parametrize(
    "result", [RuntimeError("store-text"), False], ids=["raises", "stores-nothing"]
)
def test_a_top_level_write_that_fails_keeps_the_receipt_of_an_earlier_write(
    result: Any,
) -> None:
    """Only a receipt this call left is dropped when its write stores nothing."""

    entry, renewed = _top_level_only_entry()
    hass = SimpleNamespace(data={}, config_entries=_Manager([entry], result))
    note_self_write(hass, "entry-a", _EARLIER)
    receiver = FcmReceiverHA()
    receiver.attach_hass(hass)

    receiver._persist_credentials_to_entry("entry-a", renewed)

    assert hass.config_entries.calls == 1
    assert _receipts(hass) == {"entry-a": _EARLIER}

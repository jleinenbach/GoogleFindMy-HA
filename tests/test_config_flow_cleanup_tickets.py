# tests/test_config_flow_cleanup_tickets.py
"""Lifecycle coverage for the deferred cleanup tickets of the config flow.

The staging area in ``hass.data[DOMAIN]["pending_container_cleanup"]`` is what
carries an irreversible cleanup across the gap between a CREATE_ENTRY
FlowResult and the stored config entry it only *promises*. Home Assistant
creates the entry in ``ConfigEntriesFlowManager.async_finish_flow``, after the
flow returned, so the flow cannot do the delete itself without risking a
deletion for an entry that never came to exist.

These tests cover the ticket machinery itself, independent of which job rides
on it:

* per-flow tickets, so two overlapping flows for one account stay separable;
* the removal hook (``async_remove``) on both ``ConfigFlow`` and
  ``OptionsFlowHandler``, including *why* it has to sit on the concrete class;
* the ``entry_promised`` marker that tells "no entry, ever" apart from "entry
  created, setup retrying", both of which arrive through that same hook;
* the durability gate: an update-path ticket needs a ``modified_at`` watermark,
  and the scheduler has to hand that watermark to the persistence probe;
* the fail-safe directions: no hass, no staging area, no jobs -- all no-ops.

The job used throughout is the preflight import's delete-after-import
(``imported_stable_key`` / ``imported_digest``), which is the surviving user of
this machinery: it belongs to the file handoff (track A), where Home Assistant
imports a ``secrets.json`` it found on disk and then deletes that copy. The
credential-fetching handoff that once shared these tickets was removed with PR
#1218; this file exists so its removal does not take the coverage of the
machinery with it.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
)

pytestmark = pytest.mark.asyncio

_EMAIL = "user@example.com"
_TOKEN = "aas_et/FROM_A_LOCAL_BUNDLE"
_SHARED_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"
_STABLE_KEY = "stable-key-abc"
_DIGEST = "deadbeef"


class _DeleteRecorder:
    """Collects the deletes the cleanup runner performs.

    The runner is best effort by contract, so a test that only asserted "no
    exception" would pass for a job that quietly did nothing. Recording the
    calls is what makes "the ticket was executed" and "the ticket was never
    reached" distinguishable.
    """

    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []


def _install_delete_recorder(
    monkeypatch: pytest.MonkeyPatch, recorder: _DeleteRecorder
) -> None:
    """Replace the irreversible half of the runner with a recorder.

    ``_async_delete_watched_secrets`` touches the filesystem; these tests are
    about *whether and when* it is reached, not about the delete itself.
    """

    async def _fake_delete(
        _hass: Any,
        *,
        imported_stable_key: str | None = None,
        imported_digest: str | None = None,
    ) -> None:
        recorder.delete_calls.append(
            {"stable_key": imported_stable_key, "digest": imported_digest}
        )

    monkeypatch.setattr(config_flow, "_async_delete_watched_secrets", _fake_delete)


def _import_job() -> Any:
    """Build the job a preflight import stages."""

    return config_flow.PendingContainerCleanup(
        imported_stable_key=_STABLE_KEY, imported_digest=_DIGEST
    )


def _build_hass(entries: list[Any]) -> Any:
    """Build a frame-prepared fake hass whose config_entries lists ``entries``."""

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)
            self.updated: list[dict[str, Any]] = []

        def async_get_entry(self, entry_id: str) -> Any | None:
            return next((e for e in entries if e.entry_id == entry_id), None)

        def async_entries(self, domain: str) -> list[Any]:
            return list(entries)

        def async_update_entry(self, entry: Any, **kwargs: Any) -> bool:
            self.updated.append({"entry": entry, **kwargs})
            if "data" in kwargs:
                entry.data = kwargs["data"]
            return True

        async def async_reload(self, entry_id: str) -> bool:
            return True

    class _FlowHass:
        def __init__(self) -> None:
            # ``hass.data`` is canonical on HomeAssistant and is where the flow
            # stages its deferred cleanup jobs, so the double must carry it.
            self.data: dict[str, Any] = {}
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

        async def async_add_executor_job(
            self, func: Any, *args: Any, **kwargs: Any
        ) -> Any:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        def async_create_task(self, coro: Any, *args: Any, **kwargs: Any) -> Any:
            # Close the coroutine to avoid "never awaited" warnings in tests that
            # schedule a reload; the reload itself is not under test here.
            if inspect.iscoroutine(coro):
                coro.close()
            return None

    return _FlowHass()


async def _maybe_await(result: Any) -> Any:
    """Resolve the AGENTS.md sync/async ``async_show_form`` split."""

    if inspect.isawaitable(result):
        result = await result
    return result


def _staged_cleanup(hass: Any) -> list[Any]:
    """Return the in-memory staging area the flow writes its cleanup tickets to.

    A FIFO list of per-flow tickets, not a mapping keyed by account: two
    overlapping create flows for the same account must stay separable.
    """

    bucket = hass.data.get(config_flow.DOMAIN) or {}
    staged = bucket.get(config_flow.PENDING_CONTAINER_CLEANUP_KEY) or []
    assert isinstance(staged, list)
    return staged


async def _run_staged_cleanup(
    hass: Any, *, unique_id: str | None, entry_id: str | None = None
) -> None:
    """Claim one staged ticket and execute it, as the durability gate does.

    Production never runs the jobs inline: ``async_setup_entry`` only claims the
    ticket and arms a background task that waits for proof that Home Assistant's
    storage holds the state that authorises the cleanup (see
    ``config_flow.async_schedule_pending_container_cleanup``). These tests cover
    the job semantics *after* that proof, so they drive the two halves directly
    instead of standing up HA's storage against a hand-built ``hass`` double.

    ``entry_id`` is required for the tickets of the *update* paths: those name
    their entry, and a claim that does not name the same entry must not get
    them.
    """

    jobs = config_flow._async_claim_container_cleanup(
        hass, unique_id=unique_id, entry_id=entry_id
    )
    await config_flow._async_execute_container_cleanup(hass, jobs)


def _stage_import_ticket(hass: Any, *, flow_id: str, unique_id: str | None) -> None:
    """Stage one delete-after-import job on ``flow_id``'s ticket."""

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id=flow_id,
        unique_id=unique_id,
        job=_import_job(),
    )


async def test_aborted_flow_takes_its_ticket_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser of two same-account flows must not leave its ticket behind.

    The exact interleaving Home Assistant produces: two flows for one account
    stage a ticket each, the second one is aborted (Core aborts the competing
    in-progress flow when the first finishes) and only the first ever produces
    an entry. Without the removal hook that entry would find TWO claimable
    tickets for its account and, on this or a later reload, run a cleanup that
    belongs to a flow which never created anything.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    winner = config_flow.ConfigFlow()
    winner.hass = hass  # type: ignore[assignment]
    winner.context = {}
    winner.flow_id = "flow-winner"  # type: ignore[attr-defined]
    loser = config_flow.ConfigFlow()
    loser.hass = hass  # type: ignore[assignment]
    loser.context = {}
    loser.flow_id = "flow-loser"  # type: ignore[attr-defined]

    _stage_import_ticket(
        hass, flow_id=winner._async_cleanup_ticket_id(), unique_id=_EMAIL
    )
    _stage_import_ticket(
        hass, flow_id=loser._async_cleanup_ticket_id(), unique_id=_EMAIL
    )
    assert len(_staged_cleanup(hass)) == 2

    # Home Assistant removes the aborted flow; the hook drops its ticket.
    loser.async_remove()
    assert [ticket.flow_id for ticket in _staged_cleanup(hass)] == ["flow-winner"]

    # The winner's entry claims its own ticket -- and there is nothing else to
    # claim afterwards, on this reload or any later one.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert len(recorder.delete_calls) == 1
    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert len(recorder.delete_calls) == 1


async def test_removing_a_flow_keeps_its_update_path_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update-path ticket survives the removal of the flow that staged it.

    Reauth, reconfigure and the options credential refresh all *end* by aborting
    the flow on purpose, right after they updated the entry and scheduled its
    reload. Their ticket names that entry and is waiting for the reload's
    ``async_setup_entry``; discarding it on removal would disable the cleanup on
    every update path. The distinction is exactly ``entry_id is None``.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow.flow_id = "flow-update"  # type: ignore[attr-defined]

    entry = make_config_entry(entry_id="entry-update", unique_id=_EMAIL)
    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id=flow._async_cleanup_ticket_id(),
        unique_id=_EMAIL,
        job=_import_job(),
        entry=entry,
    )

    flow.async_remove()
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == "entry-update"

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-update")
    assert len(recorder.delete_calls) == 1


async def test_removing_a_flow_keeps_the_ticket_of_a_retrying_entry_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A created entry whose FIRST setup retries must keep its ticket.

    ``ConfigEntries.async_add`` awaits ``async_setup``, so the first
    ``async_setup_entry`` runs *inside* ``async_finish_flow``, i.e. before Home
    Assistant removes the flow. When that attempt raises
    ``ConfigEntryNotReady`` it never reaches the ticket claim at the end of
    setup: Home Assistant catches the exception, schedules a retry and removes
    the flow all the same. Flow removal is therefore ambiguous -- "no entry,
    ever" and "entry created, setup retrying" arrive through the very same
    hook -- and dropping the ticket here would leave the imported
    ``secrets.json`` on disk forever, because the later successful retry finds
    nothing to claim.

    Drives the *real* ``device_selection`` step to CREATE_ENTRY so the staging
    and the promise marker are observed at their actual call sites, not through
    hand-set flags.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    # The state a preflight import leaves behind before it hands over to
    # device_selection: validated credentials and the delete job for the copy it
    # read off disk. Set here rather than driven, because the scan itself needs a
    # real file on a watched path and is covered where it lives.
    flow._auth_data = {  # type: ignore[attr-defined]
        DATA_AUTH_METHOD: "secrets_json",
        CONF_OAUTH_TOKEN: _TOKEN,
        CONF_GOOGLE_EMAIL: _EMAIL,
    }
    flow._local_bundle_pending_cleanup = _import_job()  # type: ignore[attr-defined]
    # Through the real setter: ``unique_id`` is a read-only property on the core
    # FlowHandler, and the ticket is keyed on the value it returns.
    await flow.async_set_unique_id(_EMAIL, raise_on_progress=False)

    created = await _maybe_await(flow.async_step_device_selection({}))
    assert created.get("type") == "create_entry"

    unique_id = flow.unique_id
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    # No entry id yet -- the flow cannot know it -- but the promise is recorded.
    assert staged[0].entry_id is None
    assert staged[0].entry_promised is True

    # First async_setup_entry raised ConfigEntryNotReady: nothing was claimed,
    # and Home Assistant removes the flow anyway.
    flow.async_remove()

    kept = _staged_cleanup(hass)
    assert len(kept) == 1, "a retrying entry setup must keep its cleanup ticket"
    assert kept[0].flow_id == flow._async_cleanup_ticket_id()
    assert recorder.delete_calls == []

    # The retry succeeds and claims the surviving ticket exactly once.
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.delete_calls) == 1
    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.delete_calls) == 1


async def test_removing_an_aborted_flow_still_drops_its_unpromised_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promise marker must not blunt the abort path it shares a hook with.

    Counterpart to the test above and the reason the marker is set at
    CREATE_ENTRY rather than when the ticket is staged: a flow that staged its
    jobs but never reached CREATE_ENTRY has no entry and never will, so its
    ticket must still be dropped -- otherwise a competing same-account entry
    inherits it and runs a cleanup that belongs to the aborted flow.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    aborted = config_flow.ConfigFlow()
    aborted.hass = hass  # type: ignore[assignment]
    aborted.context = {}
    aborted.flow_id = "flow-aborted"  # type: ignore[attr-defined]

    _stage_import_ticket(hass, flow_id="flow-aborted", unique_id=_EMAIL)
    assert _staged_cleanup(hass)[0].entry_promised is False

    aborted.async_remove()

    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.delete_calls == []


async def test_marking_an_entry_promise_without_a_staging_area_is_a_noop() -> None:
    """The promise marker must tolerate an empty staging area.

    A flow can reach CREATE_ENTRY without ever staging a cleanup job (manual
    credentials, nothing imported off disk), so the marker has nothing to mark.
    It must then stay silent instead of creating a bucket or raising.
    """

    hass = _build_hass([])

    assert config_flow._async_mark_cleanup_ticket_entry_promised(hass, "flow-x") == 0
    assert config_flow._async_mark_cleanup_ticket_entry_promised(None, "flow-x") == 0
    assert _staged_cleanup(hass) == []


async def test_removing_an_options_flow_drops_its_uncorrelated_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OptionsFlowHandler`` needs the same removal guard as ``ConfigFlow``.

    Today the options credential refresh only ever stages a ticket that names
    its entry, so this override is a no-op there. It exists for the next options
    path that stages an uncorrelated ticket, and without a test the guard could
    be deleted or moved without anything turning red.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    # Bypass __init__: the handler's constructor wants a config entry, and this
    # test is about the removal hook, not about flow construction.
    flow = object.__new__(config_flow.OptionsFlowHandler)
    flow.hass = hass  # type: ignore[attr-defined]
    flow.context = {}  # type: ignore[attr-defined]
    flow.flow_id = "options-flow"  # type: ignore[attr-defined]
    flow._container_cleanup_ticket_id = None  # type: ignore[attr-defined]

    staged_ok = config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id=flow._async_cleanup_ticket_id(),
        unique_id=_EMAIL,
        job=_import_job(),
        entry=None,
    )
    assert staged_ok is True
    assert len(_staged_cleanup(hass)) == 1

    # Without a hass there is nowhere to stage, and the helper must say so
    # instead of reporting a job it silently dropped.
    assert (
        config_flow._async_stage_container_cleanup_for(
            None,
            flow_id="flow-without-hass",
            unique_id=_EMAIL,
            job=_import_job(),
            entry=None,
        )
        is False
    )
    assert len(_staged_cleanup(hass)) == 1

    flow.async_remove()

    assert _staged_cleanup(hass) == []
    assert recorder.delete_calls == []


async def test_options_flow_defines_the_removal_hook_ahead_of_the_mixin() -> None:
    """Pin *why* the hook has to sit on the concrete class.

    ``data_entry_flow.FlowHandler`` precedes ``_ContainerLoginMixin`` in this
    MRO, so a mixin-level override would never be reached: Python would keep
    finding the base class's no-op first. Moving the method to the mixin would
    silently disable it, and only this assertion notices.
    """

    assert "async_remove" in config_flow.OptionsFlowHandler.__dict__

    fallback = next(
        (
            klass
            for klass in config_flow.OptionsFlowHandler.__mro__[1:]
            if "async_remove" in klass.__dict__
        ),
        None,
    )
    assert fallback is not None
    assert fallback is not config_flow._ContainerLoginMixin


async def test_an_update_ticket_is_never_claimed_by_another_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ticket that names its entry is invisible to every other entry.

    Two entries of the same account can coexist during a replace, and an entry
    without a unique id claims the account-less fallback. Neither may reach a
    ticket that was addressed to a specific entry.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    entry = make_config_entry(entry_id="entry-a", unique_id=_EMAIL)
    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=_import_job(),
        entry=entry,
    )

    # Same account, different entry: no.
    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-b")
    # Account-less fallback: no.
    await _run_staged_cleanup(hass, unique_id=None)
    # Right account, no entry id at all (the create-path claim): still no.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.delete_calls == []
    assert len(_staged_cleanup(hass)) == 1

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-a")
    assert len(recorder.delete_calls) == 1


async def test_update_cleanup_is_dropped_without_a_durability_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``modified_at`` means no proof, so the job is never staged at all.

    The gate for an update path can only be honest with a watermark: the entry
    id has been in storage since long before the update, so "the entry exists"
    would authorise the irreversible cleanup on no evidence. An entry that
    cannot supply one therefore loses its cleanup -- the imported copy survives
    on disk, which is the safe direction.
    """

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    entry = make_config_entry(entry_id="entry-no-watermark", unique_id=_EMAIL)
    del entry.modified_at

    staged = config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=_import_job(),
        entry=entry,
    )
    assert staged is False
    assert _staged_cleanup(hass) == []

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-no-watermark")
    assert recorder.delete_calls == []


async def test_scheduler_hands_the_watermark_to_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async_setup_entry`` must pass the ticket's watermark to the probe.

    The claim and the proof live in two different functions; if the watermark
    were dropped between them the update paths would silently fall back to the
    vacuous "entry id exists" proof, and every test above would still pass.
    """

    hass = _build_hass([])
    entry = make_config_entry(entry_id="entry-gate", unique_id=_EMAIL)
    watermark = entry.modified_at

    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=_import_job(),
        entry=entry,
    )

    seen: list[tuple[str, Any]] = []

    async def _fake_probe(
        _hass: Any, entry_id: str, *, min_modified_at: Any = None
    ) -> bool:
        seen.append((entry_id, min_modified_at))
        return True

    executed: list[list[Any]] = []

    async def _fake_execute(_hass: Any, jobs: list[Any]) -> None:
        executed.append(jobs)

    monkeypatch.setattr(config_flow, "_async_config_entry_is_persisted", _fake_probe)
    monkeypatch.setattr(config_flow, "_async_execute_container_cleanup", _fake_execute)

    def _create_background_task(_hass: Any, coro: Any, **_kw: Any) -> Any:
        return asyncio.ensure_future(coro)

    entry.async_create_background_task = _create_background_task

    task = config_flow.async_schedule_pending_container_cleanup(hass, entry)
    assert task is not None
    await task
    # The entry id AND the watermark reached the probe, and only then did the
    # irreversible half run.
    assert seen == [("entry-gate", watermark)]
    assert len(executed) == 1
    assert executed[0][0].imported_digest == _DIGEST


async def test_cleanup_runner_is_a_noop_without_staged_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry that never imported anything cleans up nothing."""

    recorder = _DeleteRecorder()
    _install_delete_recorder(monkeypatch, recorder)
    hass = _build_hass([])

    # No hass.data[DOMAIN] bucket at all.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    # Bucket present but no staging area.
    hass.data[config_flow.DOMAIN] = {}
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.delete_calls == []


async def test_staging_without_hass_is_a_noop() -> None:
    """Staging before the flow is bound to hass must not raise.

    Fail-safe direction: with nowhere to park the job the cleanup simply never
    happens, which leaves the credential file in place for the next import.
    """

    config_flow._async_stage_container_cleanup(
        None,
        flow_id="flow-no-hass",
        unique_id=_EMAIL,
        job=_import_job(),
    )


async def test_discovery_create_branch_marks_its_own_late_staged_ticket() -> None:
    """The post-CREATE_ENTRY staging in ``async_step_discovery`` must mark too.

    ``async_step_discovery`` stages its delete-after-import job *after*
    ``async_step_device_selection`` returned ``CREATE_ENTRY``, i.e. after the
    create path already set the promise marker. If that late staging created the
    flow's ticket (the discovery flow can reach CREATE_ENTRY without having
    staged anything before), the earlier mark applied to nothing and the ticket
    is born unmarked. Home Assistant removes the flow immediately afterwards,
    the removal hook sees an unmarked, uncorrelated ticket and drops it -- so an
    entry whose first ``async_setup_entry`` retries loses the
    delete-after-import job it was promised.

    Drives the real ``async_step_discovery`` confirm branch and replaces only
    ``async_step_device_selection`` with its CREATE_ENTRY FlowResult, because
    that is the input the branch reacts to.
    """

    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "discovery"}
    flow.flow_id = "flow-discovery"  # type: ignore[attr-defined]

    payload = config_flow.CloudDiscoveryData(
        email=_EMAIL,
        unique_id=_EMAIL,
        candidates=((_EMAIL, _TOKEN),),
        secrets_bundle={
            "google_email": _EMAIL,
            "aas_token": _TOKEN,
            "shared_key": _SHARED_HEX,
        },
    )
    flow._discovery_confirm_pending = True  # type: ignore[attr-defined]
    flow._pending_discovery_payload = payload  # type: ignore[attr-defined]
    flow._pending_discovery_updates = None  # type: ignore[attr-defined]
    flow._pending_discovery_existing_entry = None  # type: ignore[attr-defined]

    async def _created() -> Any:
        # Mirrors what the create path does right before returning: mark, then
        # hand the CREATE_ENTRY result back to async_step_discovery.
        flow._async_mark_own_cleanup_ticket_entry_promised()
        return {"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY}

    flow.async_step_device_selection = _created  # type: ignore[assignment,method-assign]

    result = await _maybe_await(flow.async_step_discovery(None))
    assert result["type"] == config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY

    staged = _staged_cleanup(hass)
    assert len(staged) == 1, "the discovery create branch should stage its job"
    assert staged[0].entry_promised is True, (
        "the job staged after CREATE_ENTRY must be marked at its own site"
    )

    # The consequence the marker exists for: the flow removal that Home
    # Assistant performs next must leave the ticket alone.
    flow.async_remove()
    assert len(_staged_cleanup(hass)) == 1

# tests/test_container_cleanup_persist_probe.py
"""Contract tests for the durability probe of the container-login cleanup.

``config_flow._async_config_entry_is_persisted`` is the single point where the
deferred, *irreversible* cleanup touches reality: it decides whether the config
entry really reached Home Assistant's storage before the login container is
told to drop the only other copy of the credentials.

Everywhere else in the suite this probe is monkeypatched, which is right for
those tests (they cover the job semantics *after* the proof) but leaves the
probe itself unpinned. A wrong storage key, a wrong storage version or a
renamed field would make it return ``False`` forever: the cleanup would never
run and every entry setup would burn the full proof timeout, silently.

These tests never hand-write the storage payload -- that would only restate the
assumption under test. They let **Home Assistant itself** serialise its config
entries through the public ``async_update_entry`` API and flush them to the real
``core.config_entries`` file, then point the probe at it. So the entry field the
probe matches on, the list it looks in, the storage key and both version numbers
all come from Home Assistant, not from this module. A wrong storage key would
find nothing, a wrong version would trip migration, a renamed field would not
match.

One harness detail is deliberately neutralised. These tests run under the
``use_real_homeassistant_modules`` fixture, which re-imports the Home Assistant
package; depending on fixture ordering the plugin's ``hass_storage`` mock ends
up patching either the pre-import or the re-imported ``Store``, so a probe may
be served from the mock *or* from the real file. The helpers below therefore
publish (and erase) the Home-Assistant-written envelope in **both** places. That
pins the behaviour of the probe rather than the plumbing of the harness, without
weakening anything: the payload is still produced entirely by Home Assistant.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is None
):  # pragma: no cover - optional dependency
    pytest.skip(
        "pytest-homeassistant-custom-component is required for the durability "
        "probe contract tests",
        allow_module_level=True,
    )

pytest_plugins = ("pytest_homeassistant_custom_component",)
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    flush_store,
)

if TYPE_CHECKING:
    from typing import Any

    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def _use_real_ha_modules(use_real_homeassistant_modules: None) -> None:
    """Activate real Home Assistant modules from the central conftest fixture."""


@pytest.fixture(autouse=True)
def _probe_against_real_storage(
    _use_real_ha_modules: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind the module under test to the real ``Store`` and storage constants.

    ``config_flow`` resolves both at import time, and whether that import
    happened under the conftest stubs or under the real package depends on which
    test module ran first in the process. Left alone, this file would pass in
    isolation and fail (or worse, pass vacuously) inside the full suite.

    Re-binding both names for the duration of the test removes that ordering
    dependency without weakening the assertions: the probe body still resolves
    the storage key, both version numbers and the entry field itself, so a drift
    in any of them still turns these tests red. Production never sees a stub, so
    nothing here papers over a real-world condition.
    """

    from homeassistant import config_entries
    from homeassistant.helpers.storage import Store

    from custom_components.googlefindmy import config_flow

    monkeypatch.setattr(config_flow, "Store", Store)
    monkeypatch.setattr(config_flow, "config_entries", config_entries)


class _StoredEntry:
    """A config entry Home Assistant stored, plus both ways to reach it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MockConfigEntry,
        path: Path,
        hass_storage: dict[str, Any],
        storage_key: str,
    ) -> None:
        self._hass = hass
        self.entry = entry
        self.path = path
        self._hass_storage = hass_storage
        self._storage_key = storage_key

    def _invalidate_store_cache(self) -> None:
        """Tell Home Assistant's store manager that the store changed.

        The manager caches which files exist in ``.storage`` and answers "does
        not exist" from that snapshot without touching the disk. Home Assistant
        invalidates the key whenever it writes -- but under the plugin's storage
        mock the write never reaches that code, so a file this test puts in
        place afterwards would stay invisible. Invalidating explicitly is what
        makes the two publication routes below equivalent; production always
        gets the invalidation for free from the real write path.
        """

        from homeassistant.helpers import storage

        storage.get_internal_store_manager(self._hass).async_invalidate(
            self._storage_key
        )

    def republish(self, payload: dict[str, Any]) -> None:
        """Make ``payload`` the stored envelope on both harness routes."""

        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self._hass_storage[self._storage_key] = payload
        self._invalidate_store_cache()

    def read_payload(self) -> dict[str, Any]:
        """Return the envelope Home Assistant wrote, from whichever route it took.

        Depending on fixture ordering the plugin's storage mock may swallow the
        write, in which case the envelope is in ``hass_storage`` and no file
        exists. Both are Home-Assistant-produced; the test only cares that it
        gets the bytes HA serialised.
        """

        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            return payload
        payload = self._hass_storage[self._storage_key]
        assert isinstance(payload, dict)
        return payload

    def erase(self) -> None:
        """Remove the stored envelope on both harness routes."""

        self.path.unlink(missing_ok=True)
        self._hass_storage.pop(self._storage_key, None)
        self._invalidate_store_cache()


async def _store_one_entry_through_home_assistant(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> _StoredEntry:
    """Let Home Assistant write one config entry to its real store.

    ``async_update_entry`` is the public API that schedules the (debounced)
    save; ``flush_store`` only collapses the debounce so the test does not have
    to travel in time.
    """

    from homeassistant import config_entries

    entry = MockConfigEntry(domain="googlefindmy", unique_id="probe@example.com")
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, title="probe")

    store = hass.config_entries._store
    await flush_store(store)

    stored = _StoredEntry(
        hass, entry, Path(store.path), hass_storage, config_entries.STORAGE_KEY
    )
    payload = stored.read_payload()

    # Sanity-check the premise of the whole test: Home Assistant really wrote an
    # envelope with entries in it. Without this, a silently empty file would let
    # the negative assertions below pass for the wrong reason.
    assert payload["data"]["entries"], "Home Assistant stored no config entries"

    stored.republish(payload)
    return stored


@pytest.mark.asyncio
async def test_probe_sees_an_entry_home_assistant_actually_stored(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """The probe finds an entry Home Assistant stored, and only that one.

    Pins storage key, both storage versions and the field the entry is
    identified by, all at once: if any of them drifted away from Home
    Assistant's real layout, the store would not load (wrong key), would raise
    on migration (wrong version) or would not match (wrong field).
    """

    from custom_components.googlefindmy import config_flow

    stored = await _store_one_entry_through_home_assistant(hass, hass_storage)

    assert await config_flow._async_config_entry_is_persisted(
        hass, stored.entry.entry_id
    )
    # A different id must not count as proof: the gate has to be entry-specific,
    # otherwise any unrelated stored entry would authorise the irreversible
    # cleanup of this one.
    assert not await config_flow._async_config_entry_is_persisted(
        hass, "an-entry-that-was-never-stored"
    )


@pytest.mark.asyncio
async def test_probe_reports_not_persisted_for_an_unwritten_store(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """No store means no proof, and therefore no cleanup.

    The fail-safe direction of the whole subsystem: without positive evidence
    the credentials stay where they are. A missing store must also not raise,
    because that path is reached on every first-ever entry of a fresh install.
    """

    from custom_components.googlefindmy import config_flow

    stored = await _store_one_entry_through_home_assistant(hass, hass_storage)
    stored.erase()

    assert not await config_flow._async_config_entry_is_persisted(
        hass, stored.entry.entry_id
    )


@pytest.mark.asyncio
async def test_probe_survives_a_malformed_store(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A payload of an unexpected shape yields ``False``, not an exception.

    A raising probe would be caught one level up and logged as "could not
    verify", which is the same fail-safe outcome, but only a plain ``False``
    keeps that warning honest about what actually happened.
    """

    from custom_components.googlefindmy import config_flow

    stored = await _store_one_entry_through_home_assistant(hass, hass_storage)
    # Keep Home Assistant's own envelope (version, minor_version, key) and
    # corrupt only the part the probe reads.
    payload = stored.read_payload()
    payload["data"] = {"entries": "not-a-list"}
    stored.republish(payload)

    assert not await config_flow._async_config_entry_is_persisted(
        hass, stored.entry.entry_id
    )


@pytest.mark.asyncio
async def test_probe_requires_a_record_at_least_as_recent_as_the_update(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """With a watermark the probe proves the *update*, not just the entry.

    This is the whole point of the watermark, and the reason the update paths
    could not simply reuse the create-path proof: the entry id is in storage
    from the moment the entry is created, so "the id is there" stays true across
    every later update and would authorise the irreversible cleanup while the
    debounced save of the new credentials is still pending.

    Driven through Home Assistant itself: ``async_update_entry`` bumps
    ``modified_at`` and schedules the save, and nothing is flushed until
    ``flush_store`` collapses the debounce. So the "still pending" state below
    is produced by Core, not simulated.
    """

    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    stored = await _store_one_entry_through_home_assistant(hass, hass_storage)
    entry_id = stored.entry.entry_id

    committed = stored.entry.modified_at
    # Equality has to count as proof: the stored record IS this update.
    assert await config_flow._async_config_entry_is_persisted(
        hass, entry_id, min_modified_at=committed
    )
    assert not await config_flow._async_config_entry_is_persisted(
        hass, entry_id, min_modified_at=committed + timedelta(seconds=1)
    )

    # Now the production sequence: update the entry, do NOT flush. The entry id
    # is still stored, so the unwatermarked probe keeps saying yes -- which is
    # exactly the vacuous proof the watermark replaces.
    hass.config_entries.async_update_entry(stored.entry, title="probe-2")
    pending = stored.entry.modified_at
    assert pending > committed
    assert await config_flow._async_config_entry_is_persisted(hass, entry_id)
    assert not await config_flow._async_config_entry_is_persisted(
        hass, entry_id, min_modified_at=pending
    )

    # Collapse the debounce: Home Assistant writes, and only now is the update
    # provably durable.
    store: Store[Any] = hass.config_entries._store
    await flush_store(store)
    stored.republish(stored.read_payload())
    assert await config_flow._async_config_entry_is_persisted(
        hass, entry_id, min_modified_at=pending
    )


@pytest.mark.asyncio
async def test_probe_rejects_a_record_without_a_parsable_modified_at(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """A record the watermark cannot be read from is "no proof", not "proof".

    Keeps the fail-safe direction when Home Assistant's storage layout drifts:
    a missing or unparsable ``modified_at`` must never fall back to the
    presence-only answer, because that is the check the watermark exists to
    replace.
    """

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    stored = await _store_one_entry_through_home_assistant(hass, hass_storage)
    watermark = stored.entry.modified_at

    for broken in ({}, {"modified_at": None}, {"modified_at": "not-a-timestamp"}):
        payload = stored.read_payload()
        record = payload["data"]["entries"][0]
        record.pop("modified_at", None)
        record.update(broken)
        stored.republish(payload)

        # Present, so the create-path proof still holds ...
        assert await config_flow._async_config_entry_is_persisted(
            hass, stored.entry.entry_id
        )
        # ... but the update-path proof does not.
        assert not await config_flow._async_config_entry_is_persisted(
            hass, stored.entry.entry_id, min_modified_at=watermark
        )

    # A *parsable* but naive timestamp is the separate failure mode: the value
    # survives ``fromisoformat`` and only the comparison against Home
    # Assistant's aware ``modified_at`` raises. The year is deliberately far in
    # the future, so a working comparison would answer "proven" -- only the
    # fail-safe TypeError branch can produce "not proven" here.
    payload = stored.read_payload()
    payload["data"]["entries"][0]["modified_at"] = "2099-01-01T00:00:00"
    stored.republish(payload)
    assert not await config_flow._async_config_entry_is_persisted(
        hass, stored.entry.entry_id, min_modified_at=watermark
    )


@pytest.mark.asyncio
async def test_home_assistant_flow_removal_drops_an_unclaimed_ticket(
    hass: HomeAssistant,
) -> None:
    """Core's flow removal really reaches our ``async_remove`` override.

    The correlation fix for two overlapping same-account flows rests entirely on
    one Home Assistant contract: ``FlowManager.async_abort`` (which
    ``ConfigEntriesFlowManager.async_finish_flow`` calls for every competing
    in-progress flow with the same unique id) routes through
    ``_async_remove_flow_progress`` and invokes the overridable
    ``FlowHandler.async_remove`` hook. Asserting that against the *real* flow
    manager is the only way to keep the fix honest: a Core rename would
    otherwise leave the ticket behind again with the whole unit-test suite still
    green.

    ``_async_add_flow_progress`` is the private counterpart of the public
    ``async_abort`` used here; there is no public way to hand an already built
    handler to the manager, and registering it is not the behaviour under test.
    """

    from homeassistant import config_entries as config_entries_module  # noqa: PLC0415

    from custom_components.googlefindmy import config_flow  # noqa: PLC0415

    flow = config_flow.ConfigFlow()
    flow.hass = hass
    flow.handler = config_flow.DOMAIN
    flow.flow_id = "flow-under-abort"
    # Core reads ``context["source"]`` while removing a flow; a user-initiated
    # config flow is the case the correlation fix is about.
    flow.context = {"source": config_entries_module.SOURCE_USER}

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id=flow._async_cleanup_ticket_id(),
        unique_id="probe@example.com",
        job=config_flow.PendingContainerCleanup(imported_digest="deadbeef"),
    )
    assert hass.data[config_flow.DOMAIN][config_flow.PENDING_CONTAINER_CLEANUP_KEY]

    manager = hass.config_entries.flow
    manager._async_add_flow_progress(flow)
    manager.async_abort(flow.flow_id)

    # The staging area is empty, so no entry of this account can inherit the
    # ticket of a flow that will never produce one.
    assert not hass.data[config_flow.DOMAIN].get(
        config_flow.PENDING_CONTAINER_CLEANUP_KEY
    )

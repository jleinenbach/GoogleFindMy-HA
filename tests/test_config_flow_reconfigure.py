"""Reconfigure flow integration tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant import config_entries

from custom_components.googlefindmy import _async_setup_new_subentries, config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
)
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    set_config_flow_unique_id,
)
from tests.test_config_flow_subentry_sync import _ConfigEntriesManagerStub, _EntryStub


def _build_reconfigure_flow(entry: _EntryStub) -> config_flow.ConfigFlow:
    flow = config_flow.ConfigFlow()
    hass = SimpleNamespace()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _EntryStub | None:
            if entry_id == entry.entry_id:
                return entry
            return None

    hass.config_entries = _ConfigEntries()
    hass.tasks: list[asyncio.Task[Any]] = []

    def _async_create_task(coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        hass.tasks.append(task)
        return task

    hass.async_create_task = _async_create_task

    flow.hass = hass  # type: ignore[assignment]
    flow.context = {
        "source": getattr(config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)
    flow._auth_data = {
        CONF_GOOGLE_EMAIL: entry.data[CONF_GOOGLE_EMAIL],
        CONF_OAUTH_TOKEN: "token",
        DATA_AUTH_METHOD: config_flow._AUTH_METHOD_INDIVIDUAL,
    }
    return flow


@pytest.mark.asyncio
async def test_reconfigure_flow_skips_already_configured_abort() -> None:
    """Reconfigure source should route to async_step_reconfigure without aborting."""

    entry = _EntryStub()
    entry.data[CONF_GOOGLE_EMAIL] = "existing@example.com"
    entry.unique_id = entry.data[CONF_GOOGLE_EMAIL]

    flow = _build_reconfigure_flow(entry)

    async def _fake_reconfigure(
        self: config_flow.ConfigFlow, user_input: dict[str, Any] | None = None
    ) -> dict[str, str]:
        return {"type": "form", "step_id": "reconfigure"}

    flow.async_step_reconfigure = _fake_reconfigure.__get__(flow, config_flow.ConfigFlow)  # type: ignore[assignment]

    result = await flow.async_step_user()

    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"


@pytest.mark.asyncio
async def test_reconfigure_reload_recreates_subentries_and_platforms() -> None:
    """Reload after reconfigure should recreate subentries with stable IDs."""

    entry = _EntryStub()
    entry.data[CONF_GOOGLE_EMAIL] = "existing@example.com"
    flow = config_flow.ConfigFlow()
    hass = SimpleNamespace()
    hass.config_entries = _ConfigEntriesManagerStub(entry)
    hass.config_entries.forward_setup_calls: list[tuple[_EntryStub, tuple[str, ...]]] = []
    hass.config_entries.setup_calls = []
    hass.verify_event_loop_thread = lambda *_args, **_kwargs: None

    async def _forward_setups(entry_to_forward: _EntryStub, platforms: tuple[str, ...]) -> None:
        hass.config_entries.forward_setup_calls.append(
            (entry_to_forward, tuple(platforms))
        )

    hass.config_entries.async_forward_entry_setups = _forward_setups  # type: ignore[attr-defined]
    hass.data = {config_flow.DOMAIN: {"entries": {entry.entry_id: entry}}}
    hass.async_create_task = asyncio.create_task
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    flow._auth_data = {
        DATA_AUTH_METHOD: "manual",
        CONF_OAUTH_TOKEN: "token",
        CONF_GOOGLE_EMAIL: entry.data[CONF_GOOGLE_EMAIL],
    }
    flow._available_devices = [("Device", "dev-1")]
    set_config_flow_unique_id(flow, None)
    context_map = flow._ensure_subentry_context()

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
            OPT_GOOGLE_HOME_FILTER_ENABLED: False,
            OPT_ENABLE_STATS_ENTITIES: True,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )

    await _async_setup_new_subentries(flow.hass, entry, entry.subentries.values())

    created_ids = [payload["config_subentry_id"] for payload in hass.config_entries.created]
    setup_calls_first = list(hass.config_entries.setup_calls)
    assert setup_calls_first, "Subentry setup should record config_subentry_id"
    assert setup_calls_first == created_ids

    expected_platforms = ("binary_sensor", "button", "device_tracker", "sensor")
    assert hass.config_entries.forward_setup_calls == [(entry, expected_platforms)]

    hass.config_entries.forward_setup_calls.clear()

    entry.subentries.clear()
    hass.config_entries.created.clear()
    hass.config_entries.setup_calls.clear()

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
            OPT_GOOGLE_HOME_FILTER_ENABLED: True,
            OPT_ENABLE_STATS_ENTITIES: True,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )

    recreated_ids = [payload["config_subentry_id"] for payload in hass.config_entries.created]

    await _async_setup_new_subentries(flow.hass, entry, entry.subentries.values())

    setup_calls_second = list(hass.config_entries.setup_calls)
    assert setup_calls_second == recreated_ids
    assert hass.config_entries.forward_setup_calls == [(entry, expected_platforms)]

    assert recreated_ids == created_ids
    assert setup_calls_first == created_ids

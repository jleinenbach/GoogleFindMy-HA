# tests/test_config_flow_initial_auth.py
"""Tests ensuring config flow initial auth preserves scoped tokens."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import config_entries as ha_config_entries
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import frame
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DOMAIN,
    OPT_CONTRIBUTOR_MODE,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_OPTIONS_SCHEMA_VERSION,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)


def test_config_flow_import_without_gpsoauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config flow must import without requiring gpsoauth at runtime."""

    monkeypatch.setitem(sys.modules, "gpsoauth", None)

    reloaded = importlib.reload(config_flow)
    flow_cls = getattr(reloaded, "ConfigFlow", None)

    assert flow_cls is not None
    assert issubclass(flow_cls, ha_config_entries.ConfigFlow)

    handlers = getattr(ha_config_entries, "HANDLERS", None)
    if handlers is not None:
        assert handlers.get(DOMAIN) is flow_cls
    else:  # Fallback for minimal stubs without handler registry
        assert getattr(flow_cls, "domain", None) == DOMAIN


def test_async_step_hub_requires_home_assistant_context() -> None:
    """Add Hub flows invoked without Home Assistant must abort."""

    async def _exercise() -> dict[str, Any]:
        hub_flow = config_flow.ConfigFlow()
        hub_flow.hass = object()  # type: ignore[assignment]
        hub_flow.context = {"source": "hub"}
        set_config_flow_unique_id(hub_flow, None)
        hub_result = await hub_flow.async_step_hub()
        if inspect.isawaitable(hub_result):
            hub_result = await hub_result
        return hub_result

    hub_result = asyncio.run(_exercise())

    assert hub_result["type"] == "abort"
    assert hub_result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_user_step_allows_additional_accounts(
    hass_executor_stub: Callable[..., SimpleNamespace],
) -> None:
    """User step should stay available when entries already exist."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "existing-entry"
            self.data = {CONF_GOOGLE_EMAIL: "existing@example.com"}

    entry = _Entry()

    # ``hass_executor_stub`` (not a bare namespace): the user step scans the
    # default watch paths through ``async_add_executor_job``, and a double
    # without it would make the preflight skip itself instead of running.
    hass = hass_executor_stub()
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: [entry] if domain == DOMAIN else []
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    result = await flow.async_step_user()
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "form"
    assert result.get("step_id") == "user"


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry_id values for tests."""

    return f"{entry_id}-{key}-subentry"


def _prepare_reconfigure_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[config_flow.ConfigFlow, Any, Any, list[tuple[Any, dict[str, Any]]]]:
    """Create a configured flow ready for reconfigure assertions."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.unique_id = "existing@example.com"
            self.title = "Existing"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
                DATA_AUTH_METHOD: config_flow._AUTH_METHOD_INDIVIDUAL,
            }
            self.options: dict[str, Any] = {
                OPT_LOCATION_POLL_INTERVAL: 300,
                OPT_DEVICE_POLL_DELAY: 10,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
                OPT_OPTIONS_SCHEMA_VERSION: 1,
            }
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _Entry | None:
            if entry_id != entry.entry_id:
                return None
            return entry

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            assert target is entry
            self.updated.append((target, dict(updates)))
            if "data" in updates:
                entry.data = dict(updates["data"])
            if "options" in updates:
                entry.options = dict(updates["options"])

        async def async_reload(
            self, entry_id: str
        ) -> None:  # pragma: no cover - overridden
            raise AssertionError("Override async_reload per test")

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.tasks: list[asyncio.Task[Any]] = []
            self.loop = asyncio.get_running_loop()

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    hass = _Hass()

    sync_calls: list[tuple[Any, dict[str, Any]]]
    sync_calls = []

    async def _fake_sync(
        self: config_flow.ConfigFlow,
        entry_obj: Any,
        *,
        options_payload: dict[str, Any],
        defaults: dict[str, Any],
        context_map: dict[str, Any],
    ) -> None:
        sync_calls.append((entry_obj, dict(options_payload)))

    async def _fake_build_api(self: config_flow.ConfigFlow) -> tuple[Any, str, str]:
        return object(), "existing@example.com", "old-token"

    async def _fake_probe(_api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_sync_feature_subentries",
        _fake_sync,
    )
    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_build_api_and_username",
        _fake_build_api,
    )
    monkeypatch.setattr(config_flow, "_try_probe_devices", _fake_probe)

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)

    return flow, entry, hass, sync_calls


def test_async_pick_working_token_accepts_guard(
    monkeypatch: pytest.MonkeyPatch,
    hass_executor_stub: Callable[..., SimpleNamespace],
) -> None:
    """Guard errors must allow candidate tokens to pass during validation."""

    attempts: list[tuple[str, str]] = []

    async def _fake_new_api(
        hass: object,
        *,
        email: str,
        token: str,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> object:
        attempts.append((email, token))
        return object()

    async def _fake_probe(api: object, *, email: str, token: str) -> None:
        # Expected flow: guard errors indicate entry-runtime constraints, so the
        # flow should accept the token immediately and defer reconciliation to
        # config entry setup where the runtime cache is available.
        raise UpdateFailed("Multiple config entries active - pass entry.runtime_data")

    monkeypatch.setattr(
        "custom_components.googlefindmy.config_flow._async_new_api_for_probe",
        _fake_new_api,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.config_flow._try_probe_devices",
        _fake_probe,
    )

    dummy_hass = hass_executor_stub()

    token = asyncio.run(
        config_flow.async_pick_working_token(
            dummy_hass,
            "guard@example.com",
            [("cache", "aas_et/GUARD")],
        )
    )

    assert token == "aas_et/GUARD"
    assert attempts == [("guard@example.com", "aas_et/GUARD")]


class _DummyResponse:
    """Minimal async context manager mimicking aiohttp.ClientResponse."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _DummyResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _DummySession:
    """Async session stub returning pre-seeded responses."""

    def __init__(self, responses: list[_DummyResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, *_args: object, **kwargs: Any) -> _DummyResponse:
        if not self._responses:
            raise AssertionError("No responses left for dummy session")
        self.calls.append({"args": _args, "kwargs": kwargs})
        return self._responses.pop(0)


class _StubCache:
    """Entry-scoped cache stub implementing the minimal async API."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.entry_id = "stub-entry"

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        if value is None:
            self._data.pop(key, None)
            return
        self._data[key] = value

    async def async_get_cached_value(self, key: str) -> Any:
        return await self.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        await self.set(key, value)

    async def get_or_set(
        self, key: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        if key in self._data:
            return self._data[key]
        result = generator()
        if asyncio.iscoroutine(result):
            result = await result
        await self.set(key, result)
        return result


@pytest.fixture
def stub_cache() -> _StubCache:
    """Provide a fresh stub cache for each test."""

    return _StubCache()


@pytest.fixture
def dummy_session_factory() -> Callable[[list[_DummyResponse]], _DummySession]:
    """Return a factory producing dummy sessions with queued responses."""

    def _factory(responses: list[_DummyResponse]) -> _DummySession:
        return _DummySession(list(responses))

    return _factory


def test_async_initial_auth_preserves_aas_token_and_uses_adm(
    monkeypatch: pytest.MonkeyPatch,
    stub_cache: _StubCache,
    dummy_session_factory: Callable[[list[_DummyResponse]], _DummySession],
) -> None:
    """Ensure config-flow device list exchange uses ADM token without mutating AAS."""

    adm_calls: list[dict[str, Any]] = []

    async def _fake_generate(username: str, *, cache: Any) -> str:
        adm_calls.append({"username": username, "cache": cache})
        return "adm-token/test"

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.adm_token_retrieval._generate_adm_token",
        _fake_generate,
    )

    processed_payloads: list[str] = []

    def _fake_process(self: GoogleFindMyAPI, payload: str) -> list[dict[str, str]]:
        processed_payloads.append(payload)
        return [{"id": "device-1"}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.api.GoogleFindMyAPI._process_device_list_response",
        _fake_process,
    )

    session = dummy_session_factory([_DummyResponse(200, b"\x10\x20")])
    aas_token = "aas_et/MASTER"

    async def _exercise() -> tuple[list[dict[str, str]], str]:
        await stub_cache.set(username_string, "User@Example.COM")
        await stub_cache.set(DATA_AAS_TOKEN, aas_token)
        api = GoogleFindMyAPI(cache=stub_cache, session=session)
        result = await api.async_get_basic_device_list(token=aas_token)
        final_aas = await stub_cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    assert adm_calls and len(adm_calls) == 1
    assert adm_calls[0]["username"] == "user@example.com"
    assert adm_calls[0]["cache"] is stub_cache

    assert session.calls, "Expected Nova request to be issued"
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-token/test"

    assert final_aas == aas_token
    assert processed_payloads == ["1020"]
    assert result == [{"id": "device-1"}]


def test_manual_config_flow_with_master_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual flow must store aas_et tokens like the secrets path."""

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert secrets_bundle is None
        return candidates[0][1] if candidates else None

    async def _fake_probe(api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        assert token.startswith("aas_et/")
        return []

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)
    monkeypatch.setattr(config_flow, "_try_probe_devices", _fake_probe)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.data: dict[str, Any] = {config_flow.DOMAIN: {}}

    captured: dict[str, Any] = {}

    async def _create_entry(
        *,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["result"] = {"title": title, "data": data, "options": options}
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "options": options,
        }

    async def _exercise() -> None:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        set_config_flow_unique_id(flow, None)

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            assert raise_on_progress is False
            set_config_flow_unique_id(flow, value)

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[assignment]
        flow.async_create_entry = _create_entry  # type: ignore[assignment]

        manual_token = "aas_et/MANUAL_MASTER"
        first = await flow.async_step_individual_tokens(
            {
                CONF_GOOGLE_EMAIL: "ManualUser@Example.COM",
                CONF_OAUTH_TOKEN: manual_token,
            }
        )
        if inspect.isawaitable(first):
            first = await first
        assert isinstance(first, dict)
        assert first.get("type") == "form"

        final = await flow.async_step_device_selection({})
        if inspect.isawaitable(final):
            final = await final
        assert isinstance(final, dict)
        assert final.get("type") == "create_entry"

    asyncio.run(_exercise())

    assert captured, "Expected config entry creation payload to be captured"
    payload = captured["result"]
    data = payload["data"]
    assert data[CONF_OAUTH_TOKEN] == "aas_et/MANUAL_MASTER"
    assert data[DATA_AAS_TOKEN] == "aas_et/MANUAL_MASTER"
    assert data[DATA_AUTH_METHOD] == config_flow._AUTH_METHOD_SECRETS


@pytest.mark.asyncio
async def test_manual_tokens_abort_when_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual token entry aborts with a clear error if dependencies are missing."""

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    flow = config_flow.ConfigFlow()
    flow.hass = SimpleNamespace(config_entries=_ConfigEntries())  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        assert raise_on_progress is False
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[assignment]

    config_flow._import_api.cache_clear()

    def _fail_import() -> type[Any]:
        raise config_flow.DependencyNotReady("dependencies missing")

    monkeypatch.setattr(config_flow, "_import_api", _fail_import)

    result = await flow.async_step_individual_tokens(
        {
            CONF_GOOGLE_EMAIL: "user@example.com",
            CONF_OAUTH_TOKEN: "oauth-token-valid-value",
        }
    )
    if inspect.isawaitable(result):
        result = await result

    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert result.get("reason") == "dependency_not_ready"


@pytest.mark.asyncio
async def test_manual_tokens_abort_when_account_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual token flow should abort when the account is already configured."""

    existing_entry = SimpleNamespace(
        entry_id="entry-existing",
        data={CONF_GOOGLE_EMAIL: "dup@example.com"},
        options={},
    )

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.entries = [existing_entry]
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return list(self.entries)

    hass = SimpleNamespace(config_entries=_ConfigEntries())

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert email == "dup@example.com"
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

    async def _never_create_entry(**_: Any) -> Mapping[str, Any]:
        raise AssertionError("async_create_entry must not be called when aborting")

    flow.async_create_entry = _never_create_entry  # type: ignore[assignment]

    with pytest.raises(data_entry_flow.AbortFlow) as captured:
        await flow.async_step_individual_tokens(
            {
                CONF_GOOGLE_EMAIL: "dup@example.com",
                CONF_OAUTH_TOKEN: "aas_et/DUPLICATE",
            }
        )

    assert captured.value.reason == "already_configured"
    assert len(hass.config_entries.entries) == 1


@pytest.mark.asyncio
async def test_device_selection_creates_and_updates_subentry() -> None:
    """Device-selection step must manage feature subentries with stable IDs."""

    class _StubEntry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.title = "Account One"
            self.data: dict[str, Any] = {CONF_GOOGLE_EMAIL: "user@example.com"}
            self.options: dict[str, Any] = {}
            self.subentries: dict[str, ConfigSubentry] = {}
            self.runtime_data = None

    class _StubConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self, entry: _StubEntry) -> None:
            self._entry = entry
            self.created: list[ConfigSubentry] = []
            self.updated: list[ConfigSubentry] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[_StubEntry]:
            if domain != DOMAIN:
                return []
            return [self._entry]

        def async_get_entry(self, entry_id: str) -> _StubEntry | None:
            if entry_id == self._entry.entry_id:
                return self._entry
            return None

        def async_create_subentry(
            self,
            entry: _StubEntry,
            *,
            data: dict[str, Any],
            title: str,
            unique_id: str | None,
            subentry_type: str,
            translation_key: str | None = None,
        ) -> ConfigSubentry:
            subentry = ConfigSubentry(
                data=MappingProxyType(dict(data)),
                subentry_type=subentry_type,
                title=title,
                unique_id=unique_id,
                subentry_id=_stable_subentry_id(entry.entry_id, data["group_key"]),
                translation_key=translation_key,
            )
            entry.subentries[subentry.subentry_id] = subentry
            self.created.append(subentry)
            return subentry

        def async_update_subentry(
            self,
            entry: _StubEntry,
            subentry: ConfigSubentry,
            *,
            data: dict[str, Any] | None = None,
            title: str | None = None,
            unique_id: str | None = None,
            translation_key: str | None = None,
        ) -> bool:
            if data is not None:
                subentry.data = MappingProxyType(dict(data))
            if title is not None:
                subentry.title = title
            if unique_id is not None:
                subentry.unique_id = unique_id
            if translation_key is not None:
                subentry.translation_key = translation_key
            entry.subentries[subentry.subentry_id] = subentry
            self.updated.append(subentry)
            return True

    class _StubHass:
        def __init__(self, entry: _StubEntry) -> None:
            prepare_flow_hass_config_entries(
                self,
                lambda: _StubConfigEntries(entry),
                frame_module=frame,
            )
            self.data: dict[str, Any] = {DOMAIN: {"entries": {entry.entry_id: {}}}}

    entry = _StubEntry()
    hass = _StubHass(entry)

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    set_config_flow_unique_id(flow, "existing")
    flow._available_devices = [("Device", "device-1")]
    flow._auth_data = {
        DATA_AUTH_METHOD: config_flow._AUTH_METHOD_SECRETS,
        CONF_OAUTH_TOKEN: "token",
        CONF_GOOGLE_EMAIL: "user@example.com",
    }

    def _create_entry(**kwargs: Any) -> dict[str, Any]:
        return {"type": "create_entry", **kwargs}

    flow.async_create_entry = _create_entry  # type: ignore[assignment]

    first_input: dict[str, Any] = {
        OPT_LOCATION_POLL_INTERVAL: 300,
        OPT_DEVICE_POLL_DELAY: 5,
        OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        OPT_CONTRIBUTOR_MODE: config_flow.CONTRIBUTOR_MODE_IN_ALL_AREAS,
    }
    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        first_input[OPT_GOOGLE_HOME_FILTER_ENABLED] = False
    if OPT_ENABLE_STATS_ENTITIES is not None:
        first_input[OPT_ENABLE_STATS_ENTITIES] = True

    result = await flow.async_step_device_selection(first_input)
    assert result["type"] == "create_entry"

    manager = hass.config_entries
    assert len(manager.created) == 2

    def _subentry_for(key: str) -> ConfigSubentry:
        for subentry in manager.created:
            if subentry.data.get("group_key") == key:
                return subentry
        raise AssertionError(f"Subentry with key {key} missing")

    tracker_subentry = _subentry_for(TRACKER_SUBENTRY_KEY)
    service_subentry = _subentry_for(SERVICE_SUBENTRY_KEY)

    assert tracker_subentry.data["group_key"] == TRACKER_SUBENTRY_KEY
    assert service_subentry.data["group_key"] == SERVICE_SUBENTRY_KEY
    assert service_subentry.data["features"] == sorted(SERVICE_FEATURE_PLATFORMS)
    assert tracker_subentry.subentry_id == _stable_subentry_id(
        entry.entry_id, TRACKER_SUBENTRY_KEY
    )
    assert service_subentry.subentry_id == _stable_subentry_id(
        entry.entry_id, SERVICE_SUBENTRY_KEY
    )
    assert service_subentry.unique_id == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"
    assert (
        flow.context["subentry_ids"][TRACKER_SUBENTRY_KEY]
        == tracker_subentry.subentry_id
    )
    assert (
        flow.context["subentry_ids"][SERVICE_SUBENTRY_KEY]
        == service_subentry.subentry_id
    )

    second_input = dict(first_input)
    second_input[OPT_MAP_VIEW_TOKEN_EXPIRATION] = False
    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        second_input[OPT_GOOGLE_HOME_FILTER_ENABLED] = True

    previous_created = len(manager.created)
    manager.updated.clear()
    result2 = await flow.async_step_device_selection(second_input)
    assert result2["type"] == "create_entry"
    assert len(manager.created) == previous_created
    assert manager.updated, "Expected tracker subentry to be updated on second run"
    updated_subentry = manager.updated[-1]
    assert updated_subentry.subentry_id == tracker_subentry.subentry_id
    assert (
        flow.context["subentry_ids"][TRACKER_SUBENTRY_KEY]
        == tracker_subentry.subentry_id
    )


def test_ephemeral_probe_cache_allows_missing_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-flow probes must tolerate ephemeral caches without entry IDs."""

    captured: dict[str, Any] = {}

    async def _fake_async_request_device_list(
        username: str,
        *,
        session: Any = None,
        cache: Any,
        token: str | None = None,
        cache_get: Callable[[str], Awaitable[Any]] | None = None,
        cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
        refresh_override: Callable[[], Awaitable[str | None]] | None = None,
        namespace: str | None = None,
    ) -> str:
        captured["username"] = username
        captured["cache"] = cache
        captured["token"] = token
        captured["namespace"] = namespace
        assert cache_get is not None
        assert cache_set is not None
        return "00"

    def _fake_process(self: GoogleFindMyAPI, result_hex: str) -> list[dict[str, Any]]:
        captured["processed_hex"] = result_hex
        return [{"id": "device"}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices.async_request_device_list",
        _fake_async_request_device_list,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.api.async_request_device_list",
        _fake_async_request_device_list,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.api.GoogleFindMyAPI._process_device_list_response",
        _fake_process,
    )

    async def _exercise() -> list[dict[str, Any]]:
        api = GoogleFindMyAPI(
            oauth_token="aas_et/PROBE", google_email="Probe@Example.com"
        )
        return await api.async_get_basic_device_list(token="aas_et/PROBE")

    result = asyncio.run(_exercise())

    assert result == [{"id": "device"}]
    assert captured["username"] == "Probe@Example.com"
    assert captured["token"] == "aas_et/PROBE"
    assert captured["namespace"] is None
    cache = captured["cache"]
    assert not hasattr(cache, "entry_id")
    assert hasattr(cache, "async_get_cached_value")
    assert hasattr(cache, "async_set_cached_value")


def test_async_step_user_reconfigure_context_bypasses_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure contexts must skip the already_configured abort and route to reconfigure."""

    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="existing@example.com",
        data={CONF_GOOGLE_EMAIL: "existing@example.com"},
        options={},
        subentries={},
    )

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()
            self.tasks: list[asyncio.Task[Any]] = []

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    flow = config_flow.ConfigFlow()
    flow.hass = _Hass()  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)

    reconfigure_calls: list[dict[str, Any] | None] = []

    async def _fake_reconfigure(
        self: config_flow.ConfigFlow, user_input: dict[str, Any] | None = None
    ) -> dict[str, str]:
        reconfigure_calls.append(user_input)
        return {"type": "form", "step_id": "reconfigure"}

    monkeypatch.setattr(
        flow,
        "async_step_reconfigure",
        _fake_reconfigure.__get__(flow, config_flow.ConfigFlow),
    )

    async def _exercise() -> dict[str, str]:
        result = await flow.async_step_user()
        if inspect.isawaitable(result):
            result = await result
        return result

    result = asyncio.run(_exercise())

    assert reconfigure_calls == [None]
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"


def test_async_step_user_bound_entry_relaxes_duplicate_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flows bound to an entry must not abort duplicate checks during reconfigure."""

    entry = SimpleNamespace(
        entry_id="entry-1",
        unique_id="existing@example.com",
        data={CONF_GOOGLE_EMAIL: "existing@example.com"},
        options={},
        subentries={},
    )

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()
            self.tasks: list[asyncio.Task[Any]] = []

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    flow = config_flow.ConfigFlow()
    flow.hass = _Hass()  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    set_config_flow_unique_id(flow, None)

    flow._auth_data[CONF_GOOGLE_EMAIL] = "existing@example.com"
    flow._auth_data[CONF_OAUTH_TOKEN] = "token"

    prepare_calls: list[bool] = []

    async def _fake_prepare_account_context(
        self: config_flow.ConfigFlow,
        *,
        email: str,
        preferred_unique_id: str | None = None,
        updates: Mapping[str, Any] | None = None,
        coalesce: bool = True,
        abort_on_duplicate: bool = True,
    ) -> None:
        prepare_calls.append(abort_on_duplicate)

    async def _fake_device_selection(
        self: config_flow.ConfigFlow, user_input: dict[str, Any] | None = None
    ) -> dict[str, str]:
        return {"type": "form", "step_id": "device_selection"}

    monkeypatch.setattr(
        flow,
        "_async_prepare_account_context",
        _fake_prepare_account_context.__get__(flow, config_flow.ConfigFlow),
    )
    monkeypatch.setattr(
        flow,
        "async_step_device_selection",
        _fake_device_selection.__get__(flow, config_flow.ConfigFlow),
    )

    result = asyncio.run(flow.async_step_user({"auth_method": None}))

    assert prepare_calls == [False]
    assert result["type"] == "form"
    assert result["step_id"] == "device_selection"


@pytest.mark.asyncio
async def test_async_step_reconfigure_awaits_reload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Reconfigure should await reload results before finishing."""

    flow, entry, hass, _ = _prepare_reconfigure_flow(monkeypatch)

    reload_calls: list[str] = []

    async def _async_reload(entry_id: str) -> bool:
        reload_calls.append(entry_id)
        await asyncio.sleep(0)
        return True

    hass.config_entries.async_reload = _async_reload  # type: ignore[assignment]

    caplog.set_level(logging.WARNING)

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        final = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, final

    initial_result, final_result = await _exercise()

    assert initial_result["type"] == "form"
    assert final_result["type"] == "abort"
    assert final_result["reason"] == "reconfigure_successful"
    assert final_result.get("description_placeholders") is None

    assert reload_calls == [entry.entry_id]
    assert not any("Reload" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_async_step_reconfigure_defers_reload_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Deferred reloads should emit warnings when they fail."""

    flow, entry, hass, _ = _prepare_reconfigure_flow(monkeypatch)

    reload_calls: list[str] = []

    def _async_reload(entry_id: str) -> bool:
        reload_calls.append(entry_id)
        if len(reload_calls) == 1:
            raise config_flow.OperationNotAllowed
        return False

    hass.config_entries.async_reload = _async_reload  # type: ignore[assignment]

    scheduled: list[tuple[Any, float]] = []

    def _immediate_call_later(
        hass_obj: Any, delay: float, action: Callable[[Any], Any]
    ) -> None:
        scheduled.append((hass_obj, delay))
        action(None)

    monkeypatch.setattr(config_flow, "async_call_later", _immediate_call_later)

    caplog.set_level(logging.WARNING)

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        final = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, final

    initial_result, final_result = await _exercise()

    assert initial_result["type"] == "form"
    assert final_result["reason"] == "reconfigure_successful"
    assert final_result["type"] == "abort"

    assert scheduled and scheduled[0][0] is hass
    assert reload_calls == [entry.entry_id, entry.entry_id]
    assert any(
        "Reload (deferred) after reconfigure for entry entry-1 returned False"
        in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_async_step_reconfigure_defers_reload_and_logs_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Deferred reload task exceptions must be logged."""

    flow, entry, hass, _ = _prepare_reconfigure_flow(monkeypatch)

    reload_calls: list[str] = []

    async def _async_reload(entry_id: str) -> Any:
        reload_calls.append(entry_id)
        if len(reload_calls) == 1:
            raise config_flow.OperationNotAllowed

        async def _raise() -> None:
            raise RuntimeError("reload boom")

        return _raise()

    hass.config_entries.async_reload = _async_reload  # type: ignore[assignment]

    scheduled_tasks: list[_FakeTask] = []

    class _FakeTask:
        def __init__(self, exc: Exception) -> None:
            self._exc = exc

        def add_done_callback(self, cb: Callable[[Any], Any]) -> None:
            cb(self)

        def result(self) -> Any:
            raise self._exc

    def _fake_create_task(coro: Any) -> _FakeTask:
        if inspect.iscoroutine(coro):
            coro.close()
        task = _FakeTask(RuntimeError("reload boom"))
        scheduled_tasks.append(task)
        return task

    hass.async_create_task = _fake_create_task  # type: ignore[assignment]

    monkeypatch.setattr(
        config_flow, "async_call_later", lambda hass_obj, delay, action: action(None)
    )

    caplog.set_level(logging.ERROR)

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        final = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, final

    initial_result, final_result = await _exercise()

    assert initial_result["type"] == "form"
    assert final_result["reason"] == "reconfigure_successful"
    assert final_result["type"] == "abort"

    assert reload_calls and reload_calls[0] == entry.entry_id
    assert scheduled_tasks
    assert any(
        "Deferred reload after reconfigure for entry entry-1 raised an exception"
        in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_async_step_reconfigure_updates_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure flows should reuse device selection and update existing entries."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.unique_id = "existing@example.com"
            self.title = "Existing"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
                DATA_AUTH_METHOD: config_flow._AUTH_METHOD_INDIVIDUAL,
            }
            self.options: dict[str, Any] = {
                OPT_LOCATION_POLL_INTERVAL: 300,
                OPT_DEVICE_POLL_DELAY: 10,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
                OPT_OPTIONS_SCHEMA_VERSION: 1,
            }
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _Entry | None:
            if entry_id != entry.entry_id:
                return None
            return entry

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            assert target is entry
            self.updated.append((target, dict(updates)))
            if "data" in updates:
                entry.data = dict(updates["data"])
            if "options" in updates:
                entry.options = dict(updates["options"])

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.tasks: list[asyncio.Task[Any]] = []

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    hass = _Hass()

    sync_calls: list[tuple[Any, dict[str, Any]]] = []

    async def _fake_sync(
        self: config_flow.ConfigFlow,
        entry_obj: Any,
        *,
        options_payload: dict[str, Any],
        defaults: dict[str, Any],
        context_map: dict[str, Any],
    ) -> None:
        sync_calls.append((entry_obj, dict(options_payload)))

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_sync_feature_subentries",
        _fake_sync,
    )

    async def _fake_build_api(self: config_flow.ConfigFlow) -> tuple[Any, str, str]:
        return object(), "existing@example.com", "old-token"

    async def _fake_probe(_api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_build_api_and_username",
        _fake_build_api,
    )
    monkeypatch.setattr(
        config_flow,
        "_try_probe_devices",
        _fake_probe,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        result = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, result

    initial_result, final_result = await _exercise()

    assert initial_result["type"] == "form"
    assert final_result["type"] == "abort"
    assert final_result["reason"] == "reconfigure_successful"

    assert sync_calls and sync_calls[0][0] is entry

    assert hass.config_entries.updated
    updated_kwargs = hass.config_entries.updated[0][1]
    assert updated_kwargs["data"][CONF_OAUTH_TOKEN] == "new-token"
    assert updated_kwargs["data"][CONF_GOOGLE_EMAIL] == "existing@example.com"
    assert updated_kwargs["options"][OPT_OPTIONS_SCHEMA_VERSION] == 2
    assert hass.config_entries.reloaded == [entry.entry_id]


@pytest.mark.asyncio
async def test_async_step_reconfigure_legacy_update_preserves_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy async_update_entry fallback must still persist options."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.unique_id = "existing@example.com"
            self.title = "Existing"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
                DATA_AUTH_METHOD: config_flow._AUTH_METHOD_INDIVIDUAL,
            }
            self.options: dict[str, Any] = {
                OPT_LOCATION_POLL_INTERVAL: 300,
                OPT_DEVICE_POLL_DELAY: 10,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
                OPT_OPTIONS_SCHEMA_VERSION: 1,
            }
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _Entry | None:
            if entry_id != entry.entry_id:
                return None
            return entry

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            assert target is entry
            update_kwargs = dict(updates)
            self.updated.append((target, update_kwargs))
            if "options" in update_kwargs:
                raise TypeError(
                    "async_update_entry() got an unexpected keyword 'options'"
                )
            if "data" in update_kwargs:
                entry.data = dict(update_kwargs["data"])

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.tasks: list[asyncio.Task[Any]] = []

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    hass = _Hass()

    sync_calls: list[tuple[Any, dict[str, Any]]] = []

    async def _fake_sync(
        self: config_flow.ConfigFlow,
        entry_obj: Any,
        *,
        options_payload: dict[str, Any],
        defaults: dict[str, Any],
        context_map: dict[str, Any],
    ) -> None:
        sync_calls.append((entry_obj, dict(options_payload)))

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_sync_feature_subentries",
        _fake_sync,
    )

    async def _fake_build_api(self: config_flow.ConfigFlow) -> tuple[Any, str, str]:
        return object(), "existing@example.com", "old-token"

    async def _fake_probe(_api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_build_api_and_username",
        _fake_build_api,
    )
    monkeypatch.setattr(
        config_flow,
        "_try_probe_devices",
        _fake_probe,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        result = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, result

    initial_result, final_result = await _exercise()

    assert initial_result["type"] == "form"
    assert final_result["type"] == "abort"
    assert final_result["reason"] == "reconfigure_successful"

    assert sync_calls and sync_calls[0][0] is entry

    assert len(hass.config_entries.updated) == 2
    first_kwargs = hass.config_entries.updated[0][1]
    fallback_kwargs = hass.config_entries.updated[1][1]
    assert "options" in first_kwargs
    assert "options" not in fallback_kwargs
    assert fallback_kwargs["data"][CONF_OAUTH_TOKEN] == "new-token"
    assert fallback_kwargs["data"][CONF_GOOGLE_EMAIL] == "existing@example.com"
    assert fallback_kwargs["data"][OPT_MAP_VIEW_TOKEN_EXPIRATION] is True

    assert entry.options[OPT_OPTIONS_SCHEMA_VERSION] == 2
    assert entry.options[OPT_LOCATION_POLL_INTERVAL] == 120
    assert entry.options[OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert hass.config_entries.reloaded == [entry.entry_id]


@pytest.mark.asyncio
async def test_async_step_reconfigure_resets_context_and_prunes_stale_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconfigure must reseed subentry context and drop stale IDs."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.unique_id = "existing@example.com"
            self.title = "Existing"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
                DATA_AUTH_METHOD: config_flow._AUTH_METHOD_INDIVIDUAL,
            }
            self.options: dict[str, Any] = {
                OPT_LOCATION_POLL_INTERVAL: 300,
                OPT_DEVICE_POLL_DELAY: 10,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
                OPT_OPTIONS_SCHEMA_VERSION: 1,
            }
            self.subentries: dict[str, ConfigSubentry] = {}

    entry = _Entry()

    tracker = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type="tracker",
        title="Tracker",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type="service",
        title="Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, SERVICE_SUBENTRY_KEY),
    )
    stale_tracker = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type="tracker",
        title="Stale tracker",
        unique_id="legacy-tracker",
        subentry_id="legacy-tracker-id",
    )
    stale_service = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY, "feature_flags": {}}),
        subentry_type="service",
        title="Stale service",
        unique_id="legacy-service",
        subentry_id="legacy-service-id",
    )

    entry.subentries[tracker.subentry_id] = tracker
    entry.subentries[service.subentry_id] = service
    entry.subentries[stale_tracker.subentry_id] = stale_tracker
    entry.subentries[stale_service.subentry_id] = stale_service

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            self.removed: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _Entry | None:
            if entry_id != entry.entry_id:
                return None
            return entry

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            assert target is entry
            self.updated.append((target, dict(updates)))
            if "data" in updates:
                entry.data = dict(updates["data"])
            if "options" in updates:
                entry.options = dict(updates["options"])

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        async def async_remove_subentry(self, target: Any, *, subentry_id: str) -> bool:
            assert target is entry
            self.removed.append(subentry_id)
            entry.subentries.pop(subentry_id, None)
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    hass = _Hass()

    async def _fake_build_api(self: config_flow.ConfigFlow) -> tuple[Any, str, str]:
        return object(), "existing@example.com", "old-token"

    async def _fake_probe(_api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "_async_build_api_and_username",
        _fake_build_api,
    )
    monkeypatch.setattr(
        config_flow,
        "_try_probe_devices",
        _fake_probe,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
        "subentry_ids": {"legacy": "legacy-id"},
    }
    set_config_flow_unique_id(flow, None)

    initial_result = await flow.async_step_reconfigure()
    result = await flow.async_step_device_selection(
        {
            OPT_LOCATION_POLL_INTERVAL: 120,
            OPT_DEVICE_POLL_DELAY: 5,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        }
    )

    assert initial_result["type"] == "form"
    assert result["type"] == "abort"
    assert result["reason"] == "reconfigure_successful"

    context_map = flow.context.get("subentry_ids", {})
    assert context_map[TRACKER_SUBENTRY_KEY] == tracker.subentry_id
    assert context_map[SERVICE_SUBENTRY_KEY] == service.subentry_id
    assert set(hass.config_entries.removed) == {
        stale_tracker.subentry_id,
        stale_service.subentry_id,
    }


# ---------------------------------------------------------------------------
# Preflight: a secrets bundle already lying at a default watch path
# ---------------------------------------------------------------------------

_PREFLIGHT_SHARED_HEX = (
    "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"
)


def _write_local_bundle(
    path: Path,
    *,
    email: str,
    token: str = "aas_et/FROM_DISK",
    age_seconds: float | None = None,
) -> Path:
    """Write an importable secrets bundle to ``path``.

    ``age_seconds`` back-dates the file. The preflight has no age limit, so this
    exists to *prove* age-blindness, not to select a bundle.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "google_email": email,
                "aas_token": token,
                "shared_key": _PREFLIGHT_SHARED_HEX,
            }
        ),
        encoding="utf-8",
    )
    if age_seconds is not None:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


def _preflight_hass(entries: list[Any]) -> Any:
    """Build a hass double with an executor and a configurable entry list."""

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return list(entries)

        def async_get_entry(self, entry_id: str) -> Any | None:
            return next((e for e in entries if e.entry_id == entry_id), None)

    class _Hass:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}
            self.config_entries = _ConfigEntries()

        async def async_add_executor_job(
            self, func: Any, *args: Any, **kwargs: Any
        ) -> Any:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

    return _Hass()


def _configured_entry(email: str) -> SimpleNamespace:
    """An existing config entry for ``email``, keyed like the flow keys itself."""

    return make_config_entry(
        entry_id=f"entry-{email}",
        unique_id=config_flow.unique_account_id(config_flow.normalize_email(email)),
        data={CONF_GOOGLE_EMAIL: email},
        options={},
        subentries={},
    )


def _pin_watch_paths(monkeypatch: pytest.MonkeyPatch, paths: list[Path]) -> None:
    """Point ``discovery._default_watch_paths`` at ``paths``.

    ``raising=True`` is the pin: the preflight is specified to scan exactly the
    paths this private helper returns, so renaming or removing it must make this
    setup fail loudly instead of leaving the tests silently scanning the real
    installation paths.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    monkeypatch.setattr(
        discovery, "_default_watch_paths", lambda: list(paths), raising=True
    )


@pytest.mark.asyncio
async def test_preflight_scan_never_runs_inside_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bundle scan is handed to ``async_add_executor_job``, not run inline.

    Measured at the production call, not mimicked in the test: the hass double
    dispatches to a real worker thread, and the patched scanner reports where it
    executed. A thread has no running event loop, so ``get_running_loop`` must
    raise there; if the flow ever called ``_scan()`` inline again, the loop would
    still be visible and this test fails.

    The other preflight tests cannot catch that regression: their executor double
    simply calls ``func()`` itself, which is indistinguishable from the flow
    calling it directly.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="offloaded@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    real_scan = discovery.scan_secrets_bundles
    where: list[str] = []

    def _scan_recording_its_context(paths: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            where.append("worker-thread")
        else:
            where.append("event-loop")
        return real_scan(paths)

    monkeypatch.setattr(
        discovery, "scan_secrets_bundles", _scan_recording_its_context, raising=True
    )

    hass = _preflight_hass([])
    # Real off-loop dispatch; the shared double would run the job inline.
    hass.async_add_executor_job = asyncio.to_thread  # type: ignore[method-assign]

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    result = await flow.async_step_user(None)

    assert where == ["worker-thread"]
    assert result["step_id"] == "found_local_bundle"


@pytest.mark.asyncio
async def test_user_step_offers_an_unconfigured_local_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bundle of an unknown account is offered, no matter how old it is."""

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json",
        email="fresh-install@example.com",
        age_seconds=400 * 24 * 3600,
    )
    _pin_watch_paths(monkeypatch, [bundle])

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        result = await flow.async_step_user(None)

    assert result["type"] == "form"
    assert result["step_id"] == "found_local_bundle"
    placeholders = result["description_placeholders"]
    assert placeholders["bundle_path"] == str(bundle)
    assert placeholders["email"] == "fresh-install@example.com"
    # Age is displayed, never used as a filter: a bundle older than a year is
    # still offered, and the placeholder reports its age.
    assert placeholders["bundle_age"].startswith("400d")
    # The single field defaults to "yes".
    schema = result["data_schema"].schema
    marker = next(iter(schema))
    assert marker.schema == "use_found_bundle"
    assert marker.default() is True
    # No plaintext account in the log (redaction is the only permitted form).
    assert "fresh-install@example.com" not in caplog.text


@pytest.mark.asyncio
async def test_user_step_skips_a_bundle_of_an_already_configured_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every found bundle already configured -> the plain auth-method form."""

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="known@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([_configured_entry("known@example.com")])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    result = await flow.async_step_user(None)

    assert result["type"] == "form"
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_user_step_offers_the_foreign_bundle_next_to_a_configured_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Multi-account: the still unknown account wins, the known one is skipped."""

    configured = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json",
        email="known@example.com",
        age_seconds=10,
    )
    foreign = _write_local_bundle(
        tmp_path / "data" / "secrets.json",
        email="second@example.com",
        age_seconds=5000,
    )
    # Newest first, so the CONFIGURED bundle is the scan winner here: skipping
    # it must not end the search.
    _pin_watch_paths(monkeypatch, [configured, foreign])

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([_configured_entry("known@example.com")])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    result = await flow.async_step_user(None)

    assert result["step_id"] == "found_local_bundle"
    assert result["description_placeholders"]["email"] == "second@example.com"
    assert result["description_placeholders"]["bundle_path"] == str(foreign)


@pytest.mark.asyncio
async def test_reconfigure_context_never_scans_for_local_bundles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reconfigure of an existing entry must not run the preflight scan.

    What is measured is the *effect* -- no scan, no marker, straight into the
    reconfigure step -- not one particular guard: ``async_step_user`` returns
    into ``async_step_reconfigure`` before the preflight block, so the block's
    own ``not is_reconfigure_context`` condition is unreachable from here and
    deliberately redundant (see the comment at that condition).
    """

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="other@example.com"
    )
    scans: list[int] = []

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    def _watch_paths() -> list[Path]:
        scans.append(1)
        return [bundle]

    monkeypatch.setattr(discovery, "_default_watch_paths", _watch_paths, raising=True)

    entry = _configured_entry("known@example.com")
    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([entry])  # type: ignore[assignment]
    flow.context = {
        "source": getattr(ha_config_entries, "SOURCE_RECONFIGURE", "reconfigure"),
        "entry_id": entry.entry_id,
    }
    set_config_flow_unique_id(flow, None)

    async def _fake_reconfigure(user_input: dict[str, Any] | None = None) -> Any:
        return {"type": "form", "step_id": "reconfigure"}

    monkeypatch.setattr(flow, "async_step_reconfigure", _fake_reconfigure)

    result = await flow.async_step_user(None)

    assert result["step_id"] == "reconfigure"
    assert scans == []
    assert flow._local_bundle_preflight_done is False


@pytest.mark.asyncio
async def test_declining_the_found_bundle_returns_to_the_user_step_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rejection lands on the auth-method form and does not loop back."""

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="decline@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    offered = await flow.async_step_user(None)
    assert offered["step_id"] == "found_local_bundle"

    declined = await flow.async_step_found_local_bundle({"use_found_bundle": False})
    assert declined["step_id"] == "user"

    # And the one-shot marker keeps a second visit on the auth-method form.
    again = await flow.async_step_user(None)
    assert again["step_id"] == "user"


@pytest.mark.asyncio
async def test_unusable_local_bundle_reshows_the_same_form_with_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An import failure must not abort the flow; the rejection stays reachable."""

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="broken@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    async def _no_working_token(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return None

    monkeypatch.setattr(config_flow, "async_pick_working_token", _no_working_token)

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    await flow.async_step_user(None)
    failed = await flow.async_step_found_local_bundle({"use_found_bundle": True})

    assert failed["type"] == "form"
    assert failed["step_id"] == "found_local_bundle"
    assert failed["errors"] == {"base": "cannot_connect"}
    assert failed["description_placeholders"]["email"] == "broken@example.com"

    # The offer is still declinable after the failure.
    declined = await flow.async_step_found_local_bundle({"use_found_bundle": False})
    assert declined["step_id"] == "user"


@pytest.mark.asyncio
async def test_accepted_local_bundle_stages_the_watcher_delete_primitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preflight import deletes through the SAME staged job as the watcher.

    Not a second delete implementation: the job carries the imported bundle's
    account + content identity, which is exactly what
    ``_async_delete_watched_secrets`` consumes behind the durability gate.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="import@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    async def _pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1] if candidates else None

    monkeypatch.setattr(config_flow, "async_pick_working_token", _pick)

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    reached: list[str] = []

    async def _fake_device_selection(user_input: dict[str, Any] | None = None) -> Any:
        reached.append("device_selection")
        return {"type": "form", "step_id": "device_selection"}

    monkeypatch.setattr(flow, "async_step_device_selection", _fake_device_selection)

    await flow.async_step_user(None)
    await flow.async_step_found_local_bundle({"use_found_bundle": True})

    assert reached == ["device_selection"]
    assert flow._auth_data[CONF_GOOGLE_EMAIL] == "import@example.com"

    # Staged, not executed: the file is still there while no entry exists.
    assert bundle.exists()
    pending = flow._local_bundle_pending_cleanup
    assert pending is not None
    scanned = discovery.read_secrets_bundle(bundle)
    assert scanned is not None
    assert pending.imported_stable_key == scanned.stable_key
    assert pending.imported_digest == scanned.digest

    # The hand-over the create path performs is the generic staging primitive.
    flow._async_stage_local_bundle_cleanup()
    tickets = flow.hass.data[config_flow.DOMAIN][
        config_flow.PENDING_CONTAINER_CLEANUP_KEY
    ]
    jobs = [job for ticket in tickets for job in ticket.jobs]
    assert len(jobs) == 1
    assert jobs[0].imported_digest == scanned.digest
    # Cleared: a second hand-over is a no-op.
    assert flow._local_bundle_pending_cleanup is None
    flow._async_stage_local_bundle_cleanup()
    assert sum(len(ticket.jobs) for ticket in tickets) == 1


@pytest.mark.asyncio
async def test_creating_the_entry_hands_the_preflight_delete_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The create path really calls the hand-over -- wiring, not primitive.

    The test above drives ``_async_stage_local_bundle_cleanup`` itself and would
    stay green if the single production call in ``async_step_device_selection``
    were deleted; the accepted bundle would then never be removed and nothing
    would notice. So this one runs the real step to CREATE_ENTRY and looks for
    the job in the gate's bucket.
    """

    from custom_components.googlefindmy import discovery  # noqa: PLC0415

    bundle = _write_local_bundle(
        tmp_path / "Auth" / "secrets.json", email="wired@example.com"
    )
    _pin_watch_paths(monkeypatch, [bundle])

    async def _pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1] if candidates else None

    monkeypatch.setattr(config_flow, "async_pick_working_token", _pick)

    flow = config_flow.ConfigFlow()
    flow.hass = _preflight_hass([])  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    await flow.async_step_user(None)
    await flow.async_step_found_local_bundle({"use_found_bundle": True})
    # No patching of async_step_device_selection here: this is the point.
    created = await flow.async_step_device_selection({})
    if inspect.isawaitable(created):
        created = await created

    assert created["type"] == "create_entry"
    tickets = flow.hass.data[config_flow.DOMAIN][
        config_flow.PENDING_CONTAINER_CLEANUP_KEY
    ]
    jobs = [job for ticket in tickets for job in ticket.jobs]
    scanned = discovery.read_secrets_bundle(bundle)
    assert scanned is not None
    assert [job.imported_digest for job in jobs] == [scanned.digest]
    # Staged, not executed: only the durability gate may remove the file.
    assert bundle.exists()

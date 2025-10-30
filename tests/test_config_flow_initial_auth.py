# tests/test_config_flow_initial_auth.py
"""Tests ensuring config flow initial auth preserves scoped tokens."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from typing import Any
from collections.abc import Awaitable, Callable
from types import MappingProxyType

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.const import (
    DOMAIN,
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    OPT_CONTRIBUTOR_MODE,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_OPTIONS_SCHEMA_VERSION,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.Auth.username_provider import username_string
from homeassistant import config_entries as ha_config_entries
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.config_entries import ConfigSubentry


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


def test_async_step_hub_delegates_to_user() -> None:
    """Add Hub flows must reuse the standard user onboarding form."""

    async def _exercise() -> tuple[Any, Any]:
        user_flow = config_flow.ConfigFlow()
        user_flow.hass = object()  # type: ignore[assignment]
        user_flow.context = {}
        user_flow.unique_id = None  # type: ignore[attr-defined]
        user_result = await user_flow.async_step_user()
        if inspect.isawaitable(user_result):
            user_result = await user_result

        hub_flow = config_flow.ConfigFlow()
        hub_flow.hass = object()  # type: ignore[assignment]
        hub_flow.context = {"source": "hub"}
        hub_flow.unique_id = None  # type: ignore[attr-defined]
        hub_result = await hub_flow.async_step_hub()
        if inspect.isawaitable(hub_result):
            hub_result = await hub_result

        return user_result, hub_result

    user_result, hub_result = asyncio.run(_exercise())

    assert hub_result == user_result
    assert hub_result.get("type") == "form"


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry_id values for tests."""

    return f"{entry_id}-{key}-subentry"


def test_async_pick_working_token_accepts_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard errors must allow candidate tokens to pass during validation."""

    attempts: list[tuple[str, str]] = []

    async def _fake_new_api(
        *, email: str, token: str, secrets_bundle: dict[str, Any] | None = None
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

    token = asyncio.run(
        config_flow.async_pick_working_token(
            "guard@example.com", [("cache", "aas_et/GUARD")]
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

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()
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
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(value: str) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda: None  # type: ignore[assignment]
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


def test_device_selection_creates_and_updates_subentry() -> None:
    """Device-selection step must manage feature subentries with stable IDs."""

    class _StubEntry:
        def __init__(self) -> None:
            self.entry_id = "entry-1"
            self.title = "Account One"
            self.data: dict[str, Any] = {CONF_GOOGLE_EMAIL: "user@example.com"}
            self.options: dict[str, Any] = {}
            self.subentries: dict[str, ConfigSubentry] = {}
            self.runtime_data = None

    class _StubConfigEntries:
        def __init__(self, entry: _StubEntry) -> None:
            self._entry = entry
            self.created: list[ConfigSubentry] = []
            self.updated: list[ConfigSubentry] = []

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
        ) -> ConfigSubentry:
            subentry = ConfigSubentry(
                data=MappingProxyType(dict(data)),
                subentry_type=subentry_type,
                title=title,
                unique_id=unique_id,
                subentry_id=_stable_subentry_id(entry.entry_id, data["group_key"]),
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
        ) -> bool:
            if data is not None:
                subentry.data = MappingProxyType(dict(data))
            if title is not None:
                subentry.title = title
            if unique_id is not None:
                subentry.unique_id = unique_id
            entry.subentries[subentry.subentry_id] = subentry
            self.updated.append(subentry)
            return True

    class _StubHass:
        def __init__(self, entry: _StubEntry) -> None:
            self.config_entries = _StubConfigEntries(entry)
            self.data: dict[str, Any] = {DOMAIN: {"entries": {entry.entry_id: {}}}}

    entry = _StubEntry()
    hass = _StubHass(entry)

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    flow.unique_id = "existing"
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
        OPT_MIN_ACCURACY_THRESHOLD: 100,
        OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        OPT_CONTRIBUTOR_MODE: config_flow.CONTRIBUTOR_MODE_IN_ALL_AREAS,
    }
    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        first_input[OPT_GOOGLE_HOME_FILTER_ENABLED] = False
    if OPT_ENABLE_STATS_ENTITIES is not None:
        first_input[OPT_ENABLE_STATS_ENTITIES] = True

    result = asyncio.run(flow.async_step_device_selection(first_input))
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
    assert flow.context["subentry_ids"][TRACKER_SUBENTRY_KEY] == tracker_subentry.subentry_id
    assert flow.context["subentry_ids"][SERVICE_SUBENTRY_KEY] == service_subentry.subentry_id

    second_input = dict(first_input)
    second_input[OPT_MAP_VIEW_TOKEN_EXPIRATION] = False
    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        second_input[OPT_GOOGLE_HOME_FILTER_ENABLED] = True

    previous_created = len(manager.created)
    manager.updated.clear()
    result2 = asyncio.run(flow.async_step_device_selection(second_input))
    assert result2["type"] == "create_entry"
    assert len(manager.created) == previous_created
    assert manager.updated, "Expected tracker subentry to be updated on second run"
    updated_subentry = manager.updated[-1]
    assert updated_subentry.subentry_id == tracker_subentry.subentry_id
    assert flow.context["subentry_ids"][TRACKER_SUBENTRY_KEY] == tracker_subentry.subentry_id


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


def test_async_step_reconfigure_updates_entry(monkeypatch: pytest.MonkeyPatch) -> None:
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
                OPT_MIN_ACCURACY_THRESHOLD: 150,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
                OPT_OPTIONS_SCHEMA_VERSION: 1,
            }
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries:
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []

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
            self.config_entries = _ConfigEntries()
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
    flow.unique_id = None  # type: ignore[attr-defined]

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any]]:
        initial = await flow.async_step_reconfigure()
        flow._auth_data[CONF_OAUTH_TOKEN] = "new-token"
        flow._auth_data[DATA_AUTH_METHOD] = config_flow._AUTH_METHOD_INDIVIDUAL
        result = await flow.async_step_device_selection(
            {
                OPT_LOCATION_POLL_INTERVAL: 120,
                OPT_DEVICE_POLL_DELAY: 5,
                OPT_MIN_ACCURACY_THRESHOLD: 90,
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            }
        )
        return initial, result

    initial_result, final_result = asyncio.run(_exercise())

    assert initial_result["type"] == "form"
    assert final_result == {"type": "abort", "reason": "reconfigure_successful"}

    assert sync_calls and sync_calls[0][0] is entry

    assert hass.config_entries.updated
    updated_kwargs = hass.config_entries.updated[0][1]
    assert updated_kwargs["data"][CONF_OAUTH_TOKEN] == "new-token"
    assert updated_kwargs["data"][CONF_GOOGLE_EMAIL] == "existing@example.com"
    assert updated_kwargs["options"][OPT_OPTIONS_SCHEMA_VERSION] == 2
    assert hass.config_entries.reloaded == [entry.entry_id]

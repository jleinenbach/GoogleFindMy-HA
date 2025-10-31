# tests/test_config_flow_discovery.py
"""Tests covering discovery-specific config flow helpers."""

from __future__ import annotations

import asyncio
import inspect
import types
from collections.abc import Callable
from typing import Any, Mapping

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
)


def test_normalize_and_validate_discovery_payload() -> None:
    """Secrets-first discovery payloads should normalize email and tokens."""

    payload = {
        "secrets_json": {
            "google_email": "DiscoveryUser@example.com",
            "aas_token": "aas_et/DISCOVERY",
            "oauth_token": "manually/PERSIST",
        }
    }

    result = config_flow._normalize_and_validate_discovery_payload(payload)

    assert result.email == "DiscoveryUser@example.com"
    assert result.unique_id == "discoveryuser@example.com"
    tokens = {token for _label, token in result.candidates}
    assert "aas_et/DISCOVERY" in tokens
    assert "manually/PERSIST" in tokens
    assert result.secrets_bundle is not None


def test_async_step_discovery_new_entry(
    monkeypatch: pytest.MonkeyPatch,
    record_flow_forms: Callable[[config_flow.ConfigFlow], list[str | None]],
) -> None:
    """Discovery for a new account should confirm before creating an entry."""

    async def _fake_pick(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert email == "new.user@example.com"
        assert secrets_bundle == {"aas_token": "aas_et/VALID_TOKEN_VALUE"}
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any], list[str | None]]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow.unique_id = None  # type: ignore[attr-defined]
        flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]

        recorded_forms = record_flow_forms(flow)

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

        payload = {
            CONF_GOOGLE_EMAIL: "new.user@example.com",
            "secrets_json": {"aas_token": "aas_et/VALID_TOKEN_VALUE"},
        }

        discovery_form = await flow.async_step_discovery(payload)
        if inspect.isawaitable(discovery_form):
            discovery_form = await discovery_form
        assert discovery_form["type"] == "form"
        assert discovery_form.get("step_id") == "discovery"
        assert flow.context.get("confirm_only") is True
        placeholders = flow.context.get("title_placeholders", {})
        assert placeholders.get("email") == "new.user@example.com"

        device_form = await flow.async_step_discovery({})
        if inspect.isawaitable(device_form):
            device_form = await device_form
        assert flow._auth_data.get(CONF_GOOGLE_EMAIL) == "new.user@example.com"  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_AUTH_METHOD) == config_flow._AUTH_METHOD_SECRETS  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_AAS_TOKEN) == "aas_et/VALID_TOKEN_VALUE"  # type: ignore[attr-defined]
        assert flow._auth_data.get(DATA_SECRET_BUNDLE) == {  # type: ignore[attr-defined]
            "aas_token": "aas_et/VALID_TOKEN_VALUE"
        }
        assert device_form["type"] == "form"
        assert device_form.get("step_id") == "device_selection"

        created_entry = await flow.async_step_device_selection({})
        if inspect.isawaitable(created_entry):
            created_entry = await created_entry

        return device_form, created_entry, recorded_forms

    device_form, created_entry, recorded_forms = asyncio.run(_exercise())
    assert device_form["type"] == "form"
    assert device_form.get("step_id") == "device_selection"
    assert created_entry["type"] == "create_entry"
    assert created_entry["data"][CONF_GOOGLE_EMAIL] == "new.user@example.com"
    assert created_entry["data"][CONF_OAUTH_TOKEN] == "aas_et/VALID_TOKEN_VALUE"
    assert created_entry["data"][DATA_AUTH_METHOD] == config_flow._AUTH_METHOD_SECRETS
    assert created_entry["data"][DATA_AAS_TOKEN] == "aas_et/VALID_TOKEN_VALUE"
    assert created_entry["data"][DATA_SECRET_BUNDLE] == {
        "aas_token": "aas_et/VALID_TOKEN_VALUE"
    }
    assert recorded_forms == ["discovery", "device_selection"]


def test_async_step_discovery_existing_entry_updates(
    monkeypatch: pytest.MonkeyPatch,
    record_flow_forms: Callable[[config_flow.ConfigFlow], list[str | None]],
) -> None:
    """Discovery for an existing entry should update data via abort helper."""

    async def _fake_pick(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _Entry:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old",
            }

    entry = _Entry()

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> tuple[
        dict[str, Any],
        dict[str, Any],
        list[dict[str, Any] | None],
        list[str | None],
    ]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        payload = {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            "candidate_tokens": ["aas_et/NEW_TOKEN_VALUE"],
        }

        normalized = config_flow._normalize_and_validate_discovery_payload(payload)
        _, updates = await config_flow._ingest_discovery_credentials(
            flow,
            normalized,
            existing_entry=entry,
        )
        assert updates is not None
        assert updates["data"][CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
        assert updates["data"].get(DATA_SECRET_BUNDLE) is None

        abort_calls: list[dict[str, Any] | None] = []
        recorded_forms = record_flow_forms(flow)

        def _abort_helper(*, updates: dict[str, Any] | None = None, **_: Any) -> None:
            abort_calls.append(updates)

        flow._abort_if_unique_id_configured = _abort_helper  # type: ignore[attr-defined]

        discovery_form = await flow.async_step_discovery(payload)
        if inspect.isawaitable(discovery_form):
            discovery_form = await discovery_form
        assert discovery_form["type"] == "form"
        assert discovery_form.get("step_id") == "discovery"
        assert not abort_calls, "abort helper should not run before confirmation"

        abort_result = await flow.async_step_discovery({})
        if inspect.isawaitable(abort_result):
            abort_result = await abort_result
        assert flow._auth_data.get(CONF_OAUTH_TOKEN) == "aas_et/NEW_TOKEN_VALUE"  # type: ignore[attr-defined]
        assert len(abort_calls) == 1
        payload = abort_calls[0]
        assert payload is not None
        data_updates = payload.get("data", {}) if isinstance(payload, dict) else {}
        assert data_updates.get(CONF_OAUTH_TOKEN) == "aas_et/NEW_TOKEN_VALUE"
        assert recorded_forms == ["discovery"]
        return discovery_form, abort_result, abort_calls, recorded_forms

    discovery_form, abort_result, abort_calls, recorded_forms = asyncio.run(_exercise())
    assert discovery_form["type"] == "form"
    assert discovery_form.get("step_id") == "discovery"
    assert len(abort_calls) == 1
    assert abort_result["type"] == "abort"
    assert abort_result["reason"] == "already_configured"


def test_async_step_discovery_update_info_existing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery-update payloads for existing entries should update and reload."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-id"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
            }
            self.unique_id = "existing@example.com"

    entry = _Entry()

    class _ConfigEntries:
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            self.lookups: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            self.lookups.append(domain)
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            self.updated.append((target, updates))

        def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    called_ingest: list[tuple[config_flow.ConfigFlow, Any]] = []

    async def _fake_ingest(
        flow: config_flow.ConfigFlow,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        called_ingest.append((flow, normalized))
        assert existing_entry is entry
        return (
            {"data": {CONF_OAUTH_TOKEN: "unused"}},
            {"data": {CONF_OAUTH_TOKEN: "aas_et/UPDATED"}},
        )

    monkeypatch.setattr(
        config_flow,
        "_ingest_discovery_credentials",
        _fake_ingest,
    )

    monkeypatch.setattr(
        config_flow,
        "_find_entry_by_email",
        lambda _hass, _email: entry,
    )

    normalized = config_flow.CloudDiscoveryData(
        email="existing@example.com",
        unique_id="existing@example.com",
        candidates=(("candidate", "aas_et/UPDATED"),),
        secrets_bundle=None,
    )

    monkeypatch.setattr(
        config_flow,
        "_normalize_and_validate_discovery_payload",
        lambda _payload: normalized,
    )

    async def _exercise() -> tuple[
        dict[str, Any],
        bool,
        list[tuple[bool]],
        list[str],
        list[tuple[Any, dict[str, Any]]],
        list[str],
    ]:
        hass = _Hass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        calls: list[tuple[bool]] = []

        def _current_entries(
            self: config_flow.ConfigFlow, *, include_ignore: bool = False
        ) -> list[Any]:
            calls.append((include_ignore,))
            assert not include_ignore
            return [entry]

        flow._async_current_entries = types.MethodType(  # type: ignore[assignment]
            _current_entries,
            flow,
        )

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        payload = {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            "candidate_tokens": ["aas_et/UPDATED"],
        }

        result = await flow.async_step_discovery_update_info(payload)
        if inspect.isawaitable(result):
            result = await result

        return (
            result,
            bool(called_ingest),
            calls,
            hass.config_entries.lookups,
            hass.config_entries.updated,
            hass.config_entries.reloaded,
        )

    result, ingest_called, calls, lookups, updates, reloaded = asyncio.run(_exercise())
    assert ingest_called, (
        f"discovery ingestion helper was not invoked: lookups={lookups!r}, result={result!r}"
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert calls, "abort helper did not inspect current entries"
    assert updates == [(entry, {"data": {CONF_OAUTH_TOKEN: "aas_et/UPDATED"}})]
    assert reloaded == [entry.entry_id]


def test_async_step_discovery_update_info_invalid_payload() -> None:
    """Invalid discovery-update payloads should abort early."""

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            return []

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> dict[str, Any]:
        hass = _Hass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        result = await flow.async_step_discovery_update_info(None)
        if inspect.isawaitable(result):
            result = await result
        return result

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"


def test_async_step_discovery_update_info_ingest_invalid_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors raised by ingestion should propagate as documented reasons."""

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "entry-id"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "old-token",
            }
            self.unique_id = "existing@example.com"

    entry = _Entry()

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

    class _Hass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _raise_ingest(*_: Any, **__: Any) -> tuple[dict[str, Any], None]:
        raise config_flow.DiscoveryFlowError("invalid_auth")

    monkeypatch.setattr(
        config_flow,
        "_ingest_discovery_credentials",
        _raise_ingest,
    )

    monkeypatch.setattr(
        config_flow,
        "_find_entry_by_email",
        lambda _hass, _email: entry,
    )

    normalized = config_flow.CloudDiscoveryData(
        email="existing@example.com",
        unique_id="existing@example.com",
        candidates=(("candidate", "aas_et/INVALID"),),
        secrets_bundle=None,
    )

    monkeypatch.setattr(
        config_flow,
        "_normalize_and_validate_discovery_payload",
        lambda _payload: normalized,
    )

    async def _exercise() -> dict[str, Any]:
        hass = _Hass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        def _current_entries(
            self: config_flow.ConfigFlow, *, include_ignore: bool = False
        ) -> list[Any]:
            assert not include_ignore
            return [entry]

        flow._async_current_entries = types.MethodType(  # type: ignore[assignment]
            _current_entries,
            flow,
        )

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        payload = {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            "candidate_tokens": ["aas_et/INVALID"],
        }

        result = await flow.async_step_discovery_update_info(payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_auth"


def test_async_step_discovery_update_alias() -> None:
    """Legacy discovery update step should forward to the update-info handler."""

    flow = config_flow.ConfigFlow()
    flow.hass = object()  # type: ignore[assignment]
    captured: dict[str, Any] = {}

    async def _fake_update_info(
        self: config_flow.ConfigFlow, info: Any
    ) -> dict[str, str]:
        captured["info"] = info
        return {"type": "form"}

    flow.async_step_discovery_update_info = types.MethodType(  # type: ignore[assignment]
        _fake_update_info,
        flow,
    )

    result = asyncio.run(flow.async_step_discovery_update({"source": "alias"}))

    assert captured["info"] == {"source": "alias"}
    assert result == {"type": "form"}


def test_async_step_discovery_routes_update_info_context() -> None:
    """Discovery context from update-info should route to the update handler."""

    flow = config_flow.ConfigFlow()
    flow.hass = object()  # type: ignore[assignment]
    flow.context = {"source": config_flow.SOURCE_DISCOVERY_UPDATE_INFO}

    captured: dict[str, Any] = {}

    async def _fake_update_info(
        self: config_flow.ConfigFlow, info: Mapping[str, Any] | None
    ) -> dict[str, str]:
        captured["info"] = info
        return {"type": "abort", "reason": "handled"}

    flow.async_step_discovery_update_info = types.MethodType(  # type: ignore[assignment]
        _fake_update_info,
        flow,
    )

    payload = {"source": "payload"}
    result = asyncio.run(flow.async_step_discovery(payload))

    assert captured["info"] == payload
    assert result == {"type": "abort", "reason": "handled"}


def test_async_step_user_confirm_only_submission() -> None:
    """Confirm-only submissions with preloaded data should advance automatically."""

    async def _exercise() -> dict[str, Any]:
        flow = config_flow.ConfigFlow()
        flow.context = {}
        flow.hass = object()  # type: ignore[assignment]
        flow._auth_data = {  # type: ignore[attr-defined]
            DATA_AUTH_METHOD: config_flow._AUTH_METHOD_SECRETS,
            CONF_GOOGLE_EMAIL: "autoconfirm@example.com",
            CONF_OAUTH_TOKEN: "aas_et/CONFIRM",
        }
        flow._available_devices = [  # type: ignore[attr-defined]
            ("Device", "device-id"),
        ]
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]
            flow._unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

        result = await flow.async_step_user({})
        if inspect.isawaitable(result):
            result = await result
        return result

    result = asyncio.run(_exercise())
    assert result["type"] == "form"


def test_async_step_discovery_invalid_payload() -> None:
    """Invalid payloads should abort with the documented reason."""

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()

    async def _exercise() -> dict[str, Any]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        return await flow.async_step_discovery({})

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"

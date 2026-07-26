# tests/test_config_flow_discovery.py
"""Tests covering discovery-specific config flow helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import types
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
    DATA_SUBENTRY_KEY,
)
from custom_components.googlefindmy.email_utils import unique_account_id
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
    set_config_flow_unique_id,
)


def test_normalize_and_validate_discovery_payload() -> None:
    """Secrets-first discovery payloads should normalize email and tokens."""

    payload = {
        "secrets_json": {
            "google_email": "DiscoveryUser@example.com",
            "aas_token": "aas_et/DISCOVERY",
            "oauth_token": "manually/PERSIST",
            # A shared_key is required to pass the single-key discovery gate.
            "shared_key": "DDEEFF",
        }
    }

    result = config_flow._normalize_and_validate_discovery_payload(payload)

    assert result.email == "DiscoveryUser@example.com"
    assert result.unique_id == unique_account_id("discoveryuser@example.com")
    tokens = {token for _label, token in result.candidates}
    assert "aas_et/DISCOVERY" in tokens
    assert "manually/PERSIST" in tokens
    assert result.secrets_bundle is not None


def test_discovery_mapping_payload_normalizes_credential_whitespace() -> None:
    """Already-parsed mapping discovery payloads must normalize credential whitespace.

    In-repo cloud discovery (discovery.py) forwards the secrets bundle as an
    already-parsed dict via ``DATA_SECRET_BUNDLE``, so it never passes through the
    str-decode branch. A stray space in ``owner_key``/``shared_key`` breaks
    AES-GCM decryption, so the mapping branch must apply the same whitespace
    normalization as the string branch.
    """

    payload = {
        DATA_SECRET_BUNDLE: {
            "google_email": "DiscoveryUser@example.com",
            "aas_token": "aas_et/DISCOVERY",
            "owner_key": "AAAA BBBB",
            # Tab and newline reproduce a line-wrapped copy/paste, the real
            # trigger for whitespace-broken credential material.
            "shared_key": "CC\tCC\nDD DD",
        }
    }

    result = config_flow._normalize_and_validate_discovery_payload(payload)

    assert result.secrets_bundle is not None
    assert result.secrets_bundle["owner_key"] == "AAAABBBB"
    assert result.secrets_bundle["shared_key"] == "CCCCDDDD"


def test_discovery_string_payload_still_normalizes_after_refactor() -> None:
    """String secrets payloads must keep normalizing after the chokepoint move.

    Normalization moved out of the str-decode branch into the shared mapping
    branch. This guards that a JSON-string bundle (the manual-paste shape) still
    gets credential whitespace stripped, so the refactor is behavior-preserving.
    """

    payload = {
        "secrets_json": json.dumps(
            {
                "google_email": "DiscoveryUser@example.com",
                "aas_token": "aas_et/DISCOVERY",
                "owner_key": "EEEE FFFF",
                # A shared_key is required to pass the single-key discovery gate.
                "shared_key": "DDEEFF",
            }
        )
    }

    result = config_flow._normalize_and_validate_discovery_payload(payload)

    assert result.secrets_bundle is not None
    assert result.secrets_bundle["owner_key"] == "EEEEFFFF"


def test_discovery_invalid_json_secrets_still_raises() -> None:
    """A malformed JSON-string bundle must still raise after the chokepoint move."""

    payload = {"secrets_json": "{not-valid-json"}

    with pytest.raises(config_flow.DiscoveryFlowError):
        config_flow._normalize_and_validate_discovery_payload(payload)


def test_async_step_discovery_new_entry(
    monkeypatch: pytest.MonkeyPatch,
    record_flow_forms: Callable[[config_flow.ConfigFlow], list[str | None]],
) -> None:
    """Discovery for a new account should confirm before creating an entry."""

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert email == "new.user@example.com"
        assert secrets_bundle == {
            "aas_token": "aas_et/VALID_TOKEN_VALUE",
            "shared_key": "DDEEFF",
        }
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

        def async_get_subentries(self, _entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    async def _exercise() -> tuple[dict[str, Any], dict[str, Any], list[str | None]]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        set_config_flow_unique_id(flow, None)
        flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]

        recorded_forms = record_flow_forms(flow)

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            set_config_flow_unique_id(flow, value)

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

        payload = {
            CONF_GOOGLE_EMAIL: "new.user@example.com",
            "secrets_json": {
                "aas_token": "aas_et/VALID_TOKEN_VALUE",
                # A shared_key is required to pass the single-key discovery gate.
                "shared_key": "DDEEFF",
            },
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
            "aas_token": "aas_et/VALID_TOKEN_VALUE",
            "shared_key": "DDEEFF",
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
        "aas_token": "aas_et/VALID_TOKEN_VALUE",
        "shared_key": "DDEEFF",
    }
    assert created_entry["data"].get(DATA_SUBENTRY_KEY) is None
    assert recorded_forms == ["discovery", "device_selection"]


def test_async_step_discovery_existing_entry_updates(
    monkeypatch: pytest.MonkeyPatch,
    record_flow_forms: Callable[[config_flow.ConfigFlow], list[str | None]],
) -> None:
    """Discovery for an existing entry should update data via abort helper."""

    async def _fake_pick(
        hass: Any,
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
            self.entry_id = "existing-entry"
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> _Entry | None:
            if entry_id == entry.entry_id:
                return entry
            return None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            resolved = self.async_get_entry(entry_id)
            if resolved is None:
                return []
            subentries = getattr(resolved, "subentries", None)
            if isinstance(subentries, dict):
                return list(subentries.values())
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

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
        set_config_flow_unique_id(flow, None)

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            set_config_flow_unique_id(flow, value)

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
        # Flat, not ``{"data": ...}``: Home Assistant merges the payload into
        # ``entry.data`` directly.
        assert "data" not in updates
        assert updates[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
        assert updates.get(DATA_SECRET_BUNDLE) is None

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

        overwrite_form = await flow.async_step_discovery({})
        if inspect.isawaitable(overwrite_form):
            overwrite_form = await overwrite_form
        # Confirming the discovery card no longer writes: the account is already
        # configured, so the flow asks before replacing anything.
        assert overwrite_form["type"] == "form"
        assert overwrite_form.get("step_id") == "discovery_overwrite"
        assert not abort_calls, "the question must be asked before the write"

        abort_result = await flow.async_step_discovery_overwrite(
            {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
        )
        if inspect.isawaitable(abort_result):
            abort_result = await abort_result
        assert flow._auth_data.get(CONF_OAUTH_TOKEN) == "aas_et/NEW_TOKEN_VALUE"  # type: ignore[attr-defined]
        assert len(abort_calls) == 1
        payload = abort_calls[0]
        assert payload is not None
        assert isinstance(payload, dict)
        assert "data" not in payload
        assert payload.get(CONF_OAUTH_TOKEN) == "aas_et/NEW_TOKEN_VALUE"
        assert recorded_forms == ["discovery", "discovery_overwrite"]
        return discovery_form, abort_result, abort_calls, recorded_forms

    discovery_form, abort_result, abort_calls, recorded_forms = asyncio.run(_exercise())
    assert discovery_form["type"] == "form"
    assert discovery_form.get("step_id") == "discovery"
    assert len(abort_calls) == 1
    assert abort_result["type"] == "abort"
    assert abort_result["reason"] == "credentials_updated"


async def _drive_discovery_overwrite(
    monkeypatch: pytest.MonkeyPatch, *, answer: bool
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Drive a discovery for an already configured account to ``answer``.

    Returns ``(entry, discovery_card, overwrite_question, result)``. The entry is
    a live object: the manager stub applies ``async_update_entry`` to it, so the
    callers can assert on ``entry.data`` instead of on the payload handed to the
    abort helper. Checking the argument is exactly what let the nested-payload
    defect through.
    """

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = "existing-entry"
            self.unique_id = unique_account_id("existing@example.com")
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                CONF_OAUTH_TOKEN: "aas_et/OLD_TOKEN_VALUE",
            }
            self.subentries: dict[str, Any] = {}
            # Home Assistant's own ``_abort_if_unique_id_configured`` runs here,
            # so the entry has to answer the attributes that helper reads.
            self.state = ConfigEntryState.LOADED
            self.source = "user"
            self.domain = config_flow.DOMAIN

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.reloaded: list[str] = []
            self.setup_calls: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_update_entry(self, target: Any, **updates: Any) -> bool:
            # Mirror Home Assistant: the ``data`` keyword replaces the mapping
            # wholesale; the merge happened in the caller.
            if "data" in updates:
                target.data = dict(updates["data"])
            return True

        def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        def async_get_entry(self, entry_id: str) -> Any | None:
            return entry if entry_id == entry.entry_id else None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    hass = _FlowHass()
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

    payload = {
        CONF_GOOGLE_EMAIL: "existing@example.com",
        "candidate_tokens": ["aas_et/NEW_TOKEN_VALUE"],
    }

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "showing the confirmation form must not write anything yet"
    )

    question = await flow.async_step_discovery({})
    if inspect.isawaitable(question):
        question = await question
    assert question["type"] == "form"
    assert question.get("step_id") == "discovery_overwrite"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "asking the question must not write anything yet"
    )

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: answer}
    )
    if inspect.isawaitable(result):
        result = await result

    return entry, form, question, result


@pytest.mark.asyncio
async def test_discovery_overwrite_confirmed_replaces_credentials_in_entry_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering yes must replace the stored credentials, flat, in ``entry.data``.

    Regression guard for the nested-payload defect. ``updates`` used to be
    ``{"data": {...}}``, but Home Assistant merges the payload *flat*
    (``data={**entry.data, **updates}``): the stored ``oauth_token`` kept its old
    value while a stray ``data`` key appeared beside it, and the resulting
    ``changed=True`` reloaded the entry, so it looked like it had worked.
    """

    entry, form, question, result = await _drive_discovery_overwrite(
        monkeypatch, answer=True
    )

    assert form.get("step_id") == "discovery"
    assert question.get("step_id") == "discovery_overwrite"
    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "the confirmed discovery must replace the stored credentials"
    )
    assert "data" not in entry.data, (
        "a nested 'data' key inside entry.data is the defect signature"
    )
    assert entry.data[CONF_GOOGLE_EMAIL] == "existing@example.com", (
        "unrelated entry data must survive the merge"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_declined_keeps_stored_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering no must write nothing and say so.

    A dialog whose rejection is never exercised is an ornament: the no-path is
    the only reason the question is worth asking.
    """

    entry, _form, _question, result = await _drive_discovery_overwrite(
        monkeypatch, answer=False
    )

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_kept", (
        "declining must not report 'already_configured' either"
    )
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "declining must leave the stored credentials untouched"
    )
    assert "data" not in entry.data


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
            self.unique_id = unique_account_id("existing@example.com")
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            self.lookups: list[str] = []
            self.setup_calls: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            self.lookups.append(domain)
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            self.updated.append((target, updates))

        def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        def async_get_entry(self, entry_id: str) -> Any | None:
            if entry_id == entry.entry_id:
                return entry
            return None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            resolved = self.async_get_entry(entry_id)
            if resolved is None:
                return []
            subentries = getattr(resolved, "subentries", None)
            if isinstance(subentries, dict):
                return list(subentries.values())
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

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
            {CONF_OAUTH_TOKEN: "aas_et/UPDATED"},
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
        unique_id=unique_account_id("existing@example.com"),
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
            set_config_flow_unique_id(flow, value)

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

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            return []

        def async_get_subentries(self, _entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

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


@pytest.mark.asyncio
async def test_async_step_discovery_update_info_reroutes_and_restores_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown discovery-update payloads should reroute and restore context."""

    normalized = config_flow.CloudDiscoveryData(
        email="user@example.org",
        unique_id=unique_account_id("user@example.org"),
        candidates=(),
        secrets_bundle=None,
    )

    monkeypatch.setattr(
        config_flow,
        "_normalize_and_validate_discovery_payload",
        lambda _payload: normalized,
    )
    monkeypatch.setattr(
        config_flow,
        "_find_entry_by_email",
        lambda _hass, _email: None,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = object()  # type: ignore[assignment]
    original_context: dict[str, Any] = {
        "source": config_flow.DISCOVERY_UPDATE_SOURCE,
        "marker": "preserve",
    }
    flow.context = original_context

    async def _set_unique_id(
        value: str,
        *,
        raise_on_progress: bool = False,
    ) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

    calls = {"discovery": 0}

    async def _fake_discovery(
        self: config_flow.ConfigFlow,
        info: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        calls["discovery"] += 1
        assert self.context.get("source") == config_flow.SOURCE_DISCOVERY
        assert info == {"payload": "value"}
        return {"type": "abort", "reason": "handled"}

    flow.async_step_discovery = types.MethodType(  # type: ignore[assignment]
        _fake_discovery,
        flow,
    )

    payload = {"payload": "value"}
    result = await flow.async_step_discovery_update_info(payload)
    assert result == {"type": "abort", "reason": "handled"}
    assert calls == {"discovery": 1}
    assert flow.context.get("source") == config_flow.DISCOVERY_UPDATE_SOURCE
    assert flow.context.get("marker") == "preserve"
    assert flow.context == original_context


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
            self.unique_id = unique_account_id("existing@example.com")
            self.subentries: dict[str, Any] = {}

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return [entry]

        def async_get_entry(self, entry_id: str) -> Any | None:
            if entry_id == entry.entry_id:
                return entry
            return None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            resolved = self.async_get_entry(entry_id)
            if resolved is None:
                return []
            subentries = getattr(resolved, "subentries", None)
            if isinstance(subentries, dict):
                return list(subentries.values())
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

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
        unique_id=unique_account_id("existing@example.com"),
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
            set_config_flow_unique_id(flow, value)

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
    flow.context = {"source": config_flow.DISCOVERY_UPDATE_SOURCE}

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
        set_config_flow_unique_id(flow, None)

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            set_config_flow_unique_id(flow, value)

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

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []

        def async_entries(self, domain: str) -> list[Any]:
            return []

        def async_get_subentries(self, _entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

    async def _exercise() -> dict[str, Any]:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}

        return await flow.async_step_discovery({})

    result = asyncio.run(_exercise())
    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"

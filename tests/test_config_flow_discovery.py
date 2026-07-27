# tests/test_config_flow_discovery.py
"""Tests covering discovery-specific config flow helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
import types
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
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
        assert discovery_form.get("step_id") == "discovery_confirm"
        assert flow.context.get("confirm_only") is True
        placeholders = flow.context.get("title_placeholders", {})
        assert placeholders.get("email") == "new.user@example.com"

        device_form = await flow.async_step_discovery_confirm({})
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
    assert recorded_forms == ["discovery_confirm", "device_selection"]


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
            if updates:
                # Mirror the core: the guard writes ``updates`` flat into the
                # entry and only then ends the flow. A double that merely
                # records models a guard that wrote nothing at all, which is a
                # real but different outcome -- and one the step must no longer
                # report as a completed overwrite.
                entry.data = {**entry.data, **updates}

        flow._abort_if_unique_id_configured = _abort_helper  # type: ignore[attr-defined]

        discovery_form = await flow.async_step_discovery(payload)
        if inspect.isawaitable(discovery_form):
            discovery_form = await discovery_form
        assert discovery_form["type"] == "form"
        assert discovery_form.get("step_id") == "discovery_confirm"
        assert not abort_calls, "abort helper should not run before confirmation"

        overwrite_form = await flow.async_step_discovery_confirm({})
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
        assert recorded_forms == ["discovery_confirm", "discovery_overwrite"]
        return discovery_form, abort_result, abort_calls, recorded_forms

    discovery_form, abort_result, abort_calls, recorded_forms = asyncio.run(_exercise())
    assert discovery_form["type"] == "form"
    assert discovery_form.get("step_id") == "discovery_confirm"
    assert len(abort_calls) == 1
    assert abort_result["type"] == "abort"
    assert abort_result["reason"] == "credentials_updated"


async def _prepare_configured_account_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any]:
    """Return ``(entry, flow)`` for an account that is already configured.

    The entry is a live object: the manager stub applies ``async_update_entry``
    to it, so callers can assert on ``entry.data`` instead of on the payload
    handed to the abort helper. Checking the argument is exactly what let the
    nested-payload defect through.
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
            # The durability watermark the delete-after-import ticket is gated
            # on. Without it the staging drops the job by design, so a stub
            # lacking it would make the cleanup assertions vacuously pass.
            self.modified_at = datetime(2026, 1, 1, tzinfo=UTC)

    entry = _Entry()

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.reloaded: list[str] = []
            self.scheduled_reloads: list[str] = []
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

        def async_schedule_reload(self, entry_id: str) -> None:
            # Home Assistant's own unique-id guard calls this after a changed
            # entry, and so does the explicit write for a bound flow.
            self.scheduled_reloads.append(entry_id)

        def async_get_entry(self, entry_id: str) -> Any | None:
            return entry if entry_id == entry.entry_id else None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            self.setup_calls.append(entry_id)
            return True

    class _FlowHass:
        def __init__(self) -> None:
            # The staging area of the delete-after-import tickets lives in
            # ``hass.data``; a stub without it could not observe them.
            self.data: dict[str, Any] = {}
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

    return entry, flow


def _discovery_payload_for_configured_account(
    discovery_source: str | None = None,
) -> dict[str, Any]:
    """Return a discovery payload carrying fresh credentials for that account."""

    payload: dict[str, Any] = {
        CONF_GOOGLE_EMAIL: "existing@example.com",
        "candidate_tokens": ["aas_et/NEW_TOKEN_VALUE"],
    }
    if discovery_source is not None:
        payload["discovery_source"] = discovery_source
    return payload


async def _drive_discovery_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: bool,
    discovery_source: str | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], Any]:
    """Drive a discovery for an already configured account to ``answer``.

    Returns ``(entry, discovery_card, overwrite_question, result, flow)``. The
    flow comes last so callers that only assert on the outcome can ignore it;
    the cleanup-staging guards need it to reach ``flow.hass.data``.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    payload = _discovery_payload_for_configured_account(discovery_source)

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "showing the confirmation form must not write anything yet"
    )

    question = await flow.async_step_discovery_confirm({})
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

    return entry, form, question, result, flow


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

    entry, form, question, result, _flow = await _drive_discovery_overwrite(
        monkeypatch, answer=True
    )

    assert form.get("step_id") == "discovery_confirm"
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

    entry, _form, _question, result, _flow = await _drive_discovery_overwrite(
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


@pytest.mark.asyncio
async def test_discovery_from_tracker_rescan_does_not_ask_to_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tracker rescan must not raise the overwrite question.

    ``device_tracker.py`` re-submits the credentials the entry already stores
    whenever it spots new trackers. Asking whether to replace them with
    themselves would be a dialog about nothing, and its text would claim a
    credentials file had been found when none was. The context source cannot
    tell the producers apart (``discovery.py`` downgrades every non-Home-
    Assistant source to plain ``discovery``), so the payload marker decides.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    payload = _discovery_payload_for_configured_account(
        config_flow.CLOUD_SCANNER_DISCOVERY_SOURCE
    )

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"
    assert form.get("step_id") == "discovery_confirm"

    result = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort", (
        "a rescan of an already configured account must not open a dialog"
    )
    assert result.get("step_id") != "discovery_overwrite"
    assert result["reason"] == "already_configured", (
        "the rescan keeps the abort behaviour it had before the dialog existed"
    )
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "the rescan still applies its payload, exactly as it did before"
    )
    assert "data" not in entry.data


@pytest.mark.asyncio
async def test_discovery_overwrite_imports_when_the_entry_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished entry must not be reported as a completed overwrite.

    The question stays on screen for as long as the user needs, and the entry
    can be deleted (or have its account identity changed) in the meantime. The
    unique-id guard then finds nothing, returns instead of aborting, and writes
    nothing at all: reporting ``credentials_updated`` there would name an event
    that did not happen. The credentials are still in hand, so the confirmed
    import continues as a first import instead of being dropped.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite"

    # The user removes the integration while the question is on screen.
    monkeypatch.setattr(
        flow.hass.config_entries, "async_entries", lambda domain: [], raising=False
    )

    device_selection_calls: list[str] = []

    async def _fake_device_selection() -> dict[str, Any]:
        device_selection_calls.append("called")
        return {"type": "form", "step_id": "device_selection"}

    monkeypatch.setattr(
        flow, "async_step_device_selection", _fake_device_selection, raising=False
    )

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert device_selection_calls == ["called"], (
        "the confirmed credentials must be imported, not dropped"
    )
    assert result["type"] == "form", (
        "an abort here would end the flow with nothing written"
    )
    assert result.get("step_id") == "device_selection"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "the removed entry must not be written to behind the user's back"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_writes_when_the_flow_is_bound_to_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow bound to the entry must still write what it reports.

    ``_async_prepare_account_context`` returns the entry unchanged when the
    flow already belongs to it (``context["entry_id"]``), because the
    unique-id guard is what normally performs the write and it is skipped in
    that case. Without an explicit write the step would report replaced
    credentials over unchanged entry data.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    flow.context = {"entry_id": entry.entry_id}
    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE"

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "reporting the replacement requires having performed it"
    )
    assert "data" not in entry.data
    assert flow.hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "the running integration keeps the old credentials until it reloads"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_writes_when_the_guard_never_matched_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An abort is not a write: an unmatched unique id must not fake success.

    ``_abort_if_unique_id_configured`` writes ``updates`` and *then* aborts, but
    it also returns silently when no entry claims the flow's unique id, which is
    what a legacy entry without one (or an account identity that has moved)
    produces. ``_async_prepare_account_context`` then raises its own
    ``AbortFlow`` at its closing line, having written nothing. Treating either
    abort as proof of a write reported ``credentials_updated`` and staged the
    discovered bundle for deletion over an entry that still held its old
    credentials: file gone, credentials unchanged.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    # A legacy entry: it matches by email, but claims no unique id, so the
    # core's guard finds nothing to write to.
    entry.unique_id = None
    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite", (
        "the account is configured, so the question must still be asked"
    )
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE"

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "the guard wrote nothing here, so the step owes the write itself"
    )
    assert "data" not in entry.data
    assert flow.hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "a write nobody reloads leaves the integration on the old credentials"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_does_not_write_twice_after_the_guard_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter-direction: a landed guard write must not be repeated.

    The step decides by reading the entry, so the check has to recognize the
    ordinary case as written. One misread and every accepted overwrite would
    write a second time and reload an entry that already holds exactly those
    credentials.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    writes: list[dict[str, Any]] = []
    original_update = flow.hass.config_entries.async_update_entry

    def _counting_update(target: Any, **updates: Any) -> bool:
        writes.append(dict(updates))
        return bool(original_update(target, **updates))

    monkeypatch.setattr(
        flow.hass.config_entries, "async_update_entry", _counting_update, raising=False
    )

    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite"
    assert not writes, "asking the question must not write anything yet"

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
    assert len(writes) == 1, (
        "the guard already wrote these credentials; writing them again is "
        "a redundant entry update and a redundant reload"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_drops_credentials_the_guard_cannot_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A superseded secrets bundle must not survive an accepted overwrite.

    ``updates`` expresses a removal by leaving an optional credential key out,
    but Home Assistant's guard merges flat (``{**entry.data, **updates}``) and
    can only add or replace. Judging the write complete from the keys present in
    ``updates`` therefore skipped the wholesale write while the old
    ``secrets_data`` was still in the entry, and the reload that follows seeds
    exactly that value into the entry-scoped token cache
    (``__init__.async_setup_entry``): old and new credentials side by side.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    # Credentials from an earlier secrets.json import. The discovered ones are
    # an individual token, so they carry no bundle and the old one is obsolete.
    entry.data[DATA_SECRET_BUNDLE] = {"owner_key": "STALE_OWNER_KEY"}
    entry.data[DATA_AAS_TOKEN] = "aas_et/OLD_TOKEN_VALUE"

    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite"

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert DATA_SECRET_BUNDLE not in entry.data, (
        "the replaced bundle stays readable for the token cache unless the "
        "step writes the payload wholesale"
    )
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
    assert entry.data[DATA_AAS_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
    assert flow.hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "the running integration keeps the old credentials until it reloads"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_rebases_onto_data_written_while_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that lands while the question is open must survive the answer.

    The payload used to be merged from ``entry.data`` when the discovery card
    was built, which is one or two user decisions before it is written. Anything
    that changed the entry in between -- the options flow refreshes credentials
    through the login container, for one -- was silently rolled back to the
    value the snapshot had captured.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    entry.data["polling_interval"] = 60

    payload = _discovery_payload_for_configured_account()

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    question = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(question):
        question = await question
    assert question.get("step_id") == "discovery_overwrite"

    # Somebody else updates the very entry this flow is about to overwrite,
    # exactly as ``_async_options_container_persist`` does.
    entry.data = {**entry.data, "polling_interval": 300}

    result = await flow.async_step_discovery_overwrite(
        {config_flow._FIELD_OVERWRITE_CREDENTIALS: True}
    )
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "the answer was yes, so the discovered credentials must land"
    )
    assert entry.data["polling_interval"] == 300, (
        "the overwrite must rebase onto the current entry data; writing the "
        "snapshot taken before the question rolls the other write back"
    )


@pytest.mark.asyncio
async def test_rebased_updates_drop_credentials_the_new_ones_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter-direction: the rebase is a full payload, not a bare delta.

    Only a full payload can *remove* a key, and credentials that no longer come
    with a secrets bundle must not leave the old bundle behind. A rebase reduced
    to "merge the new values in" would keep it and the entry would carry two
    generations of credentials at once.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    entry.data[config_flow.DATA_SECRET_BUNDLE] = {"stale": "bundle"}

    rebased = flow._async_rebase_credential_updates(
        "existing@example.com",
        {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            CONF_OAUTH_TOKEN: "aas_et/NEW_TOKEN_VALUE",
        },
    )

    assert rebased is not None
    resolved_entry, updates = rebased
    assert resolved_entry is entry
    assert updates[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
    assert config_flow.DATA_SECRET_BUNDLE not in updates, (
        "credentials without a bundle must clear the stored one"
    )


@pytest.mark.asyncio
async def test_discovery_rescan_aborts_when_the_entry_vanished_while_confirming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rescan whose account disappeared must abort, not import.

    The rescan path resolves the entry when the answer arrives rather than
    trusting the one it saw earlier. With no entry left there is nothing to
    update, and creating one is not what a rescan of a configured account
    means -- that would turn a background scan into an unrequested import.
    """

    _entry, flow = await _prepare_configured_account_flow(monkeypatch)
    payload = _discovery_payload_for_configured_account(
        discovery_source="cloud_scanner"
    )

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"

    imported = False

    async def _never_imports() -> Any:
        nonlocal imported
        imported = True
        return {"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY}

    flow.async_step_device_selection = _never_imports  # type: ignore[assignment]
    monkeypatch.setattr(
        flow.hass.config_entries, "async_entries", lambda domain: [], raising=False
    )

    result = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert not imported, "a rescan must never create an entry on its own"


@pytest.mark.asyncio
async def test_discovery_from_watched_file_update_still_asks_to_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real credential import must keep the question.

    This is the case the dialog exists for: the file watcher spots a fresh
    ``secrets.json`` for an account that is already configured. It marks such a
    payload ``discovery_update_info``, which Home Assistant does not know as a
    source, so the flow arrives here under the ordinary ``discovery`` context
    just like a rescan does. Gating on the context instead of the payload would
    have silenced exactly this dialog.
    """

    entry, form, question, result, _flow = await _drive_discovery_overwrite(
        monkeypatch,
        answer=True,
        discovery_source=config_flow.DISCOVERY_UPDATE_SOURCE,
    )

    assert form.get("step_id") == "discovery_confirm"
    assert question.get("step_id") == "discovery_overwrite"
    assert result["reason"] == "credentials_updated"
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"


def _staged_cleanup_tickets(flow: Any) -> list[Any]:
    """Return the delete-after-import tickets staged on ``flow.hass``."""

    bucket = flow.hass.data.get(config_flow.DOMAIN) or {}
    return list(bucket.get(config_flow.PENDING_CONTAINER_CLEANUP_KEY) or [])


@pytest.mark.asyncio
async def test_discovery_overwrite_confirmed_stages_the_bundle_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted overwrite must consume the bundle that carried it.

    The watcher remembers a processed bundle only in the process-local
    ``DiscoveryManager._settled_signatures``. Writing the credentials without
    staging the delete-after-import cleanup therefore ends the question for this
    Home Assistant run only: the next restart starts with an empty signature
    set, rediscovers the unchanged file and asks again, forever. The
    non-interactive update path stages exactly this ticket, and the interactive
    one has to match it.
    """

    entry, _form, _question, result, flow = await _drive_discovery_overwrite(
        monkeypatch, answer=True
    )

    assert result["reason"] == "credentials_updated"

    tickets = _staged_cleanup_tickets(flow)
    assert len(tickets) == 1, "the accepted bundle must be staged for deletion"
    ticket = tickets[0]
    assert ticket.entry_id == entry.entry_id, (
        "an update-path ticket names its entry; without that correlation Home "
        "Assistant's flow removal would discard it right after this abort"
    )
    assert ticket.min_modified_at == entry.modified_at, (
        "the durability watermark is what keeps the delete behind the store save"
    )
    assert len(ticket.jobs) == 1
    assert ticket.jobs[0].imported_stable_key is not None, (
        "a job without a stable key would not identify the bundle to remove"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_declined_keeps_the_bundle_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declined overwrite must leave the file where it is.

    The counter-direction of the guard above: staging the cleanup on both
    answers would delete the credentials the user just refused to apply, and the
    refusal only means something as long as the file stays available.
    """

    _entry, _form, _question, result, flow = await _drive_discovery_overwrite(
        monkeypatch, answer=False
    )

    assert result["reason"] == "credentials_kept"
    assert _staged_cleanup_tickets(flow) == [], (
        "declining must not schedule the deletion of the declined bundle"
    )


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


def _build_flow_for_new_account(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Return a discovery-ready flow whose account is not configured yet."""

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1]

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.setup_calls: list[str] = []
            attach_config_entries_flow_manager(self)

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

    flow = config_flow.ConfigFlow()
    flow.hass = _FlowHass()  # type: ignore[assignment]
    flow.context = {}
    set_config_flow_unique_id(flow, None)

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]
    return flow


def _discovery_payload(email: str, token: str, shared_key: str) -> dict[str, Any]:
    """Return a minimal, valid secrets-first discovery payload."""

    return {
        CONF_GOOGLE_EMAIL: email,
        "secrets_json": {
            "aas_token": token,
            # A shared_key is required to pass the single-key discovery gate.
            "shared_key": shared_key,
        },
    }


@pytest.mark.asyncio
async def test_discovery_confirmation_is_a_step_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirmation must live under its own, translatable step id.

    Regression guard for the rename. The card used to be shown under
    ``step_id="discovery"``, the id Home Assistant reserves for the *entry point*
    of a discovery flow: no core integration translates it (32 translate
    ``discovery_confirm``, none ``discovery``), and the repo guard in
    ``tests/test_manifest_translation_schema.py`` bans the key outright, so the
    card could never carry a text of its own. Under ``discovery_confirm`` the
    submit also routes to a handler of its own, which is why no state flag is
    needed to tell a submit apart from an incoming payload.
    """

    flow = _build_flow_for_new_account(monkeypatch)

    form = await flow.async_step_discovery(
        _discovery_payload("rename@example.com", "aas_et/VALID_TOKEN_VALUE", "DDEEFF")
    )
    if inspect.isawaitable(form):
        form = await form

    assert form["type"] == "form"
    assert form.get("step_id") == "discovery_confirm"

    # The submit target Home Assistant derives from that step id has to exist,
    # and it has to be a step of its own rather than the entry point re-entered.
    confirm = getattr(flow, "async_step_discovery_confirm", None)
    assert confirm is not None
    assert confirm.__func__ is not flow.async_step_discovery.__func__  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fresh_payload_supersedes_an_unanswered_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer payload must replace a card the user has not answered yet.

    This path is why the old code carried an ``is_submission`` heuristic: with a
    single handler serving both entry and submit, a second payload arriving
    mid-flow was indistinguishable from a confirmation. The rename removes the
    heuristic, so the behaviour it protected is pinned here instead.
    """

    flow = _build_flow_for_new_account(monkeypatch)

    first = await flow.async_step_discovery(
        _discovery_payload("first@example.com", "aas_et/FIRST_TOKEN_VALUE", "DDEEFF")
    )
    if inspect.isawaitable(first):
        first = await first
    assert first.get("step_id") == "discovery_confirm"
    assert flow._pending_discovery_payload is not None  # type: ignore[attr-defined]
    assert flow._pending_discovery_payload.email == "first@example.com"  # type: ignore[attr-defined]

    second = await flow.async_step_discovery(
        _discovery_payload("second@example.com", "aas_et/SECOND_TOKEN_VALUE", "AABBCC")
    )
    if inspect.isawaitable(second):
        second = await second

    assert second.get("step_id") == "discovery_confirm"
    assert flow.context.get("confirm_only") is True, (
        "the new card must be armed, not left over from the superseded one"
    )
    assert flow._pending_discovery_payload is not None  # type: ignore[attr-defined]
    assert flow._pending_discovery_payload.email == "second@example.com", (  # type: ignore[attr-defined]
        "the newer payload is the more recent truth and has to win"
    )


@pytest.mark.asyncio
async def test_discovery_confirm_without_a_pending_card_aborts() -> None:
    """Confirming when nothing is pending must abort, not raise."""

    flow = config_flow.ConfigFlow()
    flow.context = {}

    result = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "invalid_discovery_info"


@pytest.mark.asyncio
async def test_rejected_payload_does_not_leave_the_old_card_confirmable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload that fails validation must not leave the previous card armed.

    The dangerous shape is silent: the user sees the second discovery fail, and
    a confirmation arriving afterwards would apply the *first* payload, which no
    card is on screen for any more. Clearing the pending state at the top of the
    entry point is what prevents it.
    """

    flow = _build_flow_for_new_account(monkeypatch)

    first = await flow.async_step_discovery(
        _discovery_payload("first@example.com", "aas_et/FIRST_TOKEN_VALUE", "DDEEFF")
    )
    if inspect.isawaitable(first):
        first = await first
    assert first.get("step_id") == "discovery_confirm"

    rejected = await flow.async_step_discovery({"not": "a valid payload"})
    if inspect.isawaitable(rejected):
        rejected = await rejected
    assert rejected["type"] == "abort"

    assert flow._pending_discovery_payload is None, (  # type: ignore[attr-defined]
        "the superseded payload must not survive a rejected discovery"
    )

    confirmed = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(confirmed):
        confirmed = await confirmed
    assert confirmed["type"] == "abort", (
        "confirming after a rejected payload must abort, not apply the old one"
    )

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
from tests.helpers.config_entries_stub import make_config_entry
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
async def test_an_unmarked_payload_still_raises_the_overwrite_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload without a source marker must be treated as a credential import.

    This pins the failure direction, and it is the direction that matters: an
    unknown producer gets asked about rather than trusted, because replacing
    working credentials unasked is the worse failure. A future rebuild onto a
    positive list of trusted sources would invert exactly this and silently
    kill the credential import, so it has to fail here.

    The former counterpart of this test covered the tracker rescan, which no
    longer produces discovery payloads at all; the flow context cannot tell
    producers apart anyway (``discovery.py`` downgrades every non-Home-
    Assistant source to plain ``discovery``).
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    payload = _discovery_payload_for_configured_account()
    assert "discovery_source" not in payload

    form = await flow.async_step_discovery(payload)
    if inspect.isawaitable(form):
        form = await form
    assert form["type"] == "form"
    assert form.get("step_id") == "discovery_confirm"

    result = await flow.async_step_discovery_confirm({})
    if inspect.isawaitable(result):
        result = await result

    assert result.get("step_id") == "discovery_overwrite", (
        "an unmarked payload must be asked about, never applied silently"
    )
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/OLD_TOKEN_VALUE", (
        "nothing may be written while the question is still on screen"
    )


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
async def test_discovery_overwrite_reloads_after_a_guard_write_on_an_unloaded_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guard write on an entry without a listener still has to take effect.

    The guard is asked not to reload (``reload_on_update=False``) because the
    entry's update listener carries that. An entry that is not loaded has none
    left: Home Assistant removes the listener on unload, which is the state
    expired credentials produce, and precisely the entry a discovery overwrite
    is meant to rescue. Leaving the reload to that absent listener reported
    ``credentials_updated`` while the account kept running on the old
    credentials until a restart, and the bundle staged for deletion was never
    consumed either, because the durability gate lives in ``async_setup_entry``.

    The state below names the scenario, it does not drive it: the step schedules
    the reload state-blind, on purpose. Deciding by state would break the case
    where the merge is a no-op (an unchanged bundle after a restart), because
    there no listener fires either and the cleanup ticket would stay unredeemed.
    Whether one reload or two arrive is settled by the shared latch, not here.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    # No listener: the core removed it when the entry was unloaded after
    # ``ConfigEntryAuthFailed``. Nothing but this flow can reload it.
    entry.state = ConfigEntryState.SETUP_ERROR

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
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE"
    assert flow.hass.config_entries.scheduled_reloads == [entry.entry_id], (
        "the guard wrote but does not reload; without a listener nobody else "
        "makes those credentials effective"
    )


@pytest.mark.asyncio
async def test_discovery_overwrite_stands_down_when_the_guard_write_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter-direction: a claimed reload must not be scheduled twice.

    Where the entry is loaded, its update listener reloads on the same write.
    Both go through the shared latch, so whoever claims first reloads and the
    other stands down; ``async_schedule_reload`` does not coalesce.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)

    integration = config_flow.import_integration_package()
    assert integration.claim_pending_entry_reload(flow.hass, entry.entry_id) is True

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
    assert entry.data[CONF_OAUTH_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "standing down concerns the reload only; the credentials are written either way"
    )
    assert flow.hass.config_entries.scheduled_reloads == [], (
        "a reload is already on its way; a second one only tears the entry "
        "down twice in a row"
    )
    assert _staged_cleanup_tickets(flow), (
        "the bundle has to be staged for deletion regardless of who reloads; "
        "tying the staging to the claim would leave it rediscovered forever"
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
async def test_discovery_overwrite_leaves_the_reload_to_a_claim_already_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer stands down when a reload of that entry is already on its way.

    Writing credentials notifies the integration's update listener, which reloads
    the entry so they take effect. ``async_schedule_reload`` does not coalesce, so
    the write and the listener would unload and set the entry up twice in a row
    unless both go through the same latch.
    """

    entry, flow = await _prepare_configured_account_flow(monkeypatch)
    # A superseded bundle is what the core's flat merge cannot drop, which is
    # exactly the case the fallback writer exists for.
    entry.data[DATA_SECRET_BUNDLE] = {"owner_key": "STALE_OWNER_KEY"}
    entry.data[DATA_AAS_TOKEN] = "aas_et/OLD_TOKEN_VALUE"

    integration = config_flow.import_integration_package()
    assert integration.claim_pending_entry_reload(flow.hass, entry.entry_id) is True

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
    assert entry.data[DATA_AAS_TOKEN] == "aas_et/NEW_TOKEN_VALUE", (
        "the credentials still have to be written; only the reload is deferred"
    )
    assert flow.hass.config_entries.scheduled_reloads == [], (
        "a reload is already on its way; a second one only tears the entry down twice"
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
            self.reload_calls: list[str] = []
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
            # ``reloaded`` is de-duplicated by the flow's own bookkeeping, so it
            # cannot show a second reload of the same entry. This list is not,
            # and that is what a coalescing claim has to be measured against.
            self.reload_calls.append(entry_id)

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
            # The reload latch lives in ``hass.data``; without the bucket the
            # claim helper falls back to "reload anyway" and the second run
            # below could not observe the coalescing.
            self.data: dict[str, Any] = {}

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

        # A second payload while the first reload is still on its way must not
        # add another unload/setup cycle: ``async_schedule_reload`` does not
        # coalesce, and the entry update notifies the credential update listener
        # as well.
        repeat = await flow.async_step_discovery_update_info(payload)
        if inspect.isawaitable(repeat):
            repeat = await repeat

        return (
            result,
            bool(called_ingest),
            calls,
            hass.config_entries.lookups,
            hass.config_entries.updated,
            hass.config_entries.reloaded,
            hass.config_entries.reload_calls,
        )

    result, ingest_called, calls, lookups, updates, reloaded, reload_calls = (
        asyncio.run(_exercise())
    )
    assert ingest_called, (
        f"discovery ingestion helper was not invoked: lookups={lookups!r}, result={result!r}"
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"
    assert calls, "abort helper did not inspect current entries"
    assert updates == [(entry, {"data": {CONF_OAUTH_TOKEN: "aas_et/UPDATED"}})]
    assert reloaded == [entry.entry_id]
    assert reload_calls == [entry.entry_id], (
        "the second payload arrived while the first reload was still on its way; "
        "a second one only unloads and sets the entry up twice"
    )


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


def test_the_reload_latch_helper_fails_open() -> None:
    """Bookkeeping must never be the reason a written credential stays inert.

    ``_claim_entry_reload`` answers "you reload" whenever it cannot get a
    trustworthy answer: without an entry id, against an integration package that
    does not carry the latch, and when consulting it raises. One reload too many
    is a nuisance; a missing one leaves the new credentials stored but
    ineffective until the next restart.
    """

    hass = types.SimpleNamespace(data={})

    assert config_flow._claim_entry_reload(hass, "") is True

    original = config_flow.import_integration_package

    try:

        def _without_latch() -> Any:
            return types.SimpleNamespace()

        config_flow.import_integration_package = _without_latch  # type: ignore[assignment]
        assert config_flow._claim_entry_reload(hass, "entry-1") is True

        def _raise() -> Any:
            raise RuntimeError("integration package unavailable")

        config_flow.import_integration_package = _raise  # type: ignore[assignment]
        assert config_flow._claim_entry_reload(hass, "entry-1") is True
    finally:
        config_flow.import_integration_package = original  # type: ignore[assignment]


def test_the_latch_release_helper_reaches_the_latch_and_stays_quiet() -> None:
    """Giving the latch back must work, and must never raise into a flow.

    The counterpart of ``_claim_entry_reload``: a claim that cannot be honoured
    has to be released, or the latch stays set for the lifetime of the process
    and swallows every later reload of that entry. Because it runs on the error
    path of an already failing operation, it stays silent on an empty id, on an
    integration package without the latch, and when consulting it raises.
    """

    hass = types.SimpleNamespace(data={})
    released: list[tuple[Any, str]] = []

    original = config_flow.import_integration_package

    try:

        def _with_latch() -> Any:
            return types.SimpleNamespace(
                discard_pending_entry_reload=lambda h, entry_id: released.append(
                    (h, entry_id)
                )
            )

        config_flow.import_integration_package = _with_latch  # type: ignore[assignment]
        config_flow._discard_entry_reload(hass, "entry-1")
        assert released == [(hass, "entry-1")]

        released.clear()
        config_flow._discard_entry_reload(hass, "")
        assert released == [], "an empty id has no latch to give back"

        def _without_latch() -> Any:
            return types.SimpleNamespace()

        config_flow.import_integration_package = _without_latch  # type: ignore[assignment]
        config_flow._discard_entry_reload(hass, "entry-1")

        def _raise() -> Any:
            raise RuntimeError("integration package unavailable")

        config_flow.import_integration_package = _raise  # type: ignore[assignment]
        config_flow._discard_entry_reload(hass, "entry-1")
    finally:
        config_flow.import_integration_package = original  # type: ignore[assignment]


class _FinishedTask:
    """A task that has already ended, with a chosen outcome.

    The real sites hand a live ``asyncio.Task`` to the helper; what the helper
    actually needs is only the done-callback protocol, and driving that
    synchronously keeps the outcome under the test's control instead of the
    event loop's.

    ``add_done_callback`` catches what the callback raises and records it in
    ``callback_error`` instead of letting it travel back to the caller. A real
    ``asyncio.Future`` hands the callback to ``loop.call_soon``, so an exception
    inside it reaches the loop's exception handler and never the code that
    registered it. Without that isolation the helper's *outer* ``suppress``
    would swallow every escape and a test could not tell whether the callback
    itself handles a task that refuses to answer.
    """

    def __init__(
        self,
        *,
        cancelled: bool = False,
        exception: BaseException | None = None,
        callback_raises: bool = False,
        answer_raises: bool = False,
        result: Any = True,
        result_raises: bool = False,
    ) -> None:
        self._cancelled = cancelled
        self._exception = exception
        self._callback_raises = callback_raises
        self._answer_raises = answer_raises
        self._result = result
        self._result_raises = result_raises
        self.callback_error: BaseException | None = None

    def cancelled(self) -> bool:
        if self._answer_raises:
            raise RuntimeError("this future refuses to answer")
        return self._cancelled

    def exception(self) -> BaseException | None:
        # A real future raises here for a cancelled task instead of answering
        # ``None``. Mirroring that matters: it is what makes the guard in the
        # callback load-bearing rather than cosmetic, because ``CancelledError``
        # derives from ``BaseException`` and would pass an ``except Exception``.
        if self._answer_raises:
            raise RuntimeError("this future refuses to answer")
        if self._cancelled:
            raise asyncio.CancelledError
        return self._exception

    def result(self) -> Any:
        # ``async_reload`` reports a failed unload, and a component it could not
        # set up, by *returning* ``False`` rather than by raising, so the
        # callback has to read the outcome as well as the exception. A double
        # without this method would leave that branch untested here.
        if self._answer_raises or self._result_raises:
            raise RuntimeError("this future refuses to answer")
        if self._exception is not None:
            raise self._exception
        return self._result

    def add_done_callback(self, callback: Callable[[Any], None]) -> None:
        if self._callback_raises:
            raise RuntimeError("this stub takes no callbacks")
        try:
            callback(self)
        except BaseException as err:  # noqa: BLE001 - the loop would absorb it too
            self.callback_error = err


def test_a_direct_reload_that_never_lands_gives_the_latch_back() -> None:
    """A claim is a promise to reload; a dead task has to release it.

    ``_schedule_claimed_reload`` covers only the synchronous half: a scheduling
    call that raises releases the latch right there. A path that reloads
    *directly* keeps the promise open for its task's whole lifetime, and that
    task can still end without a reload -- ``async_unload`` rejects an entry in
    a lifecycle state that forbids it, and the flow task dies with it. None of
    the release points (unload, setup, removal) runs then, so the latch would
    stay set for the life of the process and every later credential write would
    stand down with its new credentials ineffective.
    """

    hass = types.SimpleNamespace(data={})
    released: list[str] = []

    original = config_flow.import_integration_package

    def _with_latch() -> Any:
        return types.SimpleNamespace(
            discard_pending_entry_reload=lambda _h, entry_id: released.append(entry_id)
        )

    try:
        config_flow.import_integration_package = _with_latch  # type: ignore[assignment]

        config_flow._release_claim_when_reload_fails(
            hass, "entry-1", _FinishedTask(exception=RuntimeError("no reload"))
        )
        assert released == ["entry-1"]

        released.clear()
        config_flow._release_claim_when_reload_fails(
            hass, "entry-2", _FinishedTask(cancelled=True)
        )
        assert released == ["entry-2"], "a cancelled reload is no reload either"

        released.clear()
        config_flow._release_claim_when_reload_fails(hass, "entry-3", _FinishedTask())
        assert released == [], (
            "the reload arrived; unload and setup release the latch themselves"
        )

        released.clear()
        config_flow._release_claim_when_reload_fails(
            hass, "entry-4", _FinishedTask(result=False)
        )
        assert released == ["entry-4"], (
            "no exception is not the same as reloaded: async_reload reports a "
            "failed unload, and a component it could not set up, by returning "
            "False. None of the release points runs then either, so a latch "
            "kept here would be just as permanent as after a raising task"
        )
    finally:
        config_flow.import_integration_package = original  # type: ignore[assignment]


def test_the_release_helper_stays_quiet_on_a_task_that_cannot_answer() -> None:
    """It runs on the error path of a failing operation and must never raise.

    Three shapes a test double can take: no callback protocol at all, one that
    rejects the callback, and one that refuses to report its outcome. None of
    them may turn a failed reload into a failed flow, and none of them may
    release a latch on a guess either.
    """

    hass = types.SimpleNamespace(data={})
    released: list[str] = []

    original = config_flow.import_integration_package

    def _with_latch() -> Any:
        return types.SimpleNamespace(
            discard_pending_entry_reload=lambda _h, entry_id: released.append(entry_id)
        )

    try:
        config_flow.import_integration_package = _with_latch  # type: ignore[assignment]

        config_flow._release_claim_when_reload_fails(hass, "entry-1", object())
        config_flow._release_claim_when_reload_fails(
            hass, "entry-2", _FinishedTask(callback_raises=True)
        )
        mute = _FinishedTask(answer_raises=True)
        config_flow._release_claim_when_reload_fails(hass, "entry-3", mute)

        # Two more shapes, both introduced with the outcome check: a double from
        # before that check exists (no ``result`` at all) and one that answers
        # ``cancelled``/``exception`` but refuses to report its outcome. Neither
        # says the reload failed, so neither may release a latch on a guess.
        class _NoResult:
            def cancelled(self) -> bool:
                return False

            def exception(self) -> BaseException | None:
                return None

            def add_done_callback(self, callback: Callable[[Any], None]) -> None:
                callback(self)

        config_flow._release_claim_when_reload_fails(hass, "entry-4", _NoResult())
        coy = _FinishedTask(result_raises=True)
        config_flow._release_claim_when_reload_fails(hass, "entry-5", coy)
        assert coy.callback_error is None, (
            "a task that refuses to report its outcome must not escape the "
            "callback either"
        )

        assert released == []
        assert mute.callback_error is None, (
            "the done-callback has to absorb a task that refuses to answer "
            "itself; a real loop would only log the escape, so the helper's "
            "outer suppress never sees it"
        )
    finally:
        config_flow.import_integration_package = original  # type: ignore[assignment]


def test_a_discovery_update_reload_that_is_rejected_gives_the_latch_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-interactive discovery update reloads directly, so it must clean up.

    ``async_unload`` rejects an entry in a lifecycle state that forbids it, and
    the reload task dies with that rejection. None of the release points
    (unload, setup, entry removal) runs then, so a latch kept here would be
    permanent: every later credential write would see the stale claim, stand
    down, and leave its newly stored credentials ineffective until a restart.
    """

    # Canonical stub per tests/AGENTS.md: the factory guarantees ``data`` and
    # ``options`` are plain dicts, which an ad-hoc class does not.
    entry = make_config_entry(
        entry_id="entry-id",
        data={
            CONF_GOOGLE_EMAIL: "existing@example.com",
            CONF_OAUTH_TOKEN: "old-token",
        },
        unique_id=unique_account_id("existing@example.com"),
        subentries={},
    )

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            return [entry]

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            self.updated.append((target, updates))

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)
            raise config_flow.OperationNotAllowed(entry_id)

        def async_get_entry(self, entry_id: str) -> Any | None:
            return entry if entry_id == entry.entry_id else None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.data: dict[str, Any] = {}
            self.reload_coros: list[Any] = []

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            # Held instead of run: the test decides when the reload task ends,
            # so the failure is observed rather than raced.
            self.reload_coros.append(coro)
            return None

    async def _fake_ingest(
        flow: config_flow.ConfigFlow,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return (
            {"data": {CONF_OAUTH_TOKEN: "unused"}},
            {CONF_OAUTH_TOKEN: "aas_et/UPDATED"},
        )

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _fake_ingest)
    monkeypatch.setattr(config_flow, "_find_entry_by_email", lambda _h, _e: entry)
    monkeypatch.setattr(
        config_flow,
        "_normalize_and_validate_discovery_payload",
        lambda _payload: config_flow.CloudDiscoveryData(
            email="existing@example.com",
            unique_id=unique_account_id("existing@example.com"),
            candidates=(("candidate", "aas_et/UPDATED"),),
            secrets_bundle=None,
        ),
    )

    async def _exercise() -> tuple[Any, bool, bool]:
        hass = _Hass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow._async_current_entries = types.MethodType(  # type: ignore[assignment]
            lambda self, *, include_ignore=False: [entry], flow
        )

        async def _set_unique_id(
            value: str, *, raise_on_progress: bool = False
        ) -> None:
            set_config_flow_unique_id(flow, value)

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

        result = await flow.async_step_discovery_update_info(
            {
                CONF_GOOGLE_EMAIL: "existing@example.com",
                "candidate_tokens": ["aas_et/UPDATED"],
            }
        )
        if inspect.isawaitable(result):
            result = await result

        integration = config_flow.import_integration_package()
        assert hass.reload_coros, "the flow did not reload directly at all"
        held_before = (
            integration.claim_pending_entry_reload(hass, entry.entry_id) is False
        )

        with pytest.raises(config_flow.OperationNotAllowed):
            await hass.reload_coros[0]

        free_after = integration.claim_pending_entry_reload(hass, entry.entry_id)
        return result, held_before, free_after

    result, held_before, free_after = asyncio.run(_exercise())

    assert result["type"] == "abort"
    assert held_before, "the flow has to claim the latch before reloading directly"
    assert free_after, (
        "the reload was rejected, so no release point ever ran; a latch kept "
        "here suppresses every later credential reload of this entry"
    )


async def _drive_discovery_update_reload(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reload_result: Any,
    loaded_components: tuple[str, ...],
) -> tuple[Any, Any, Any, Any]:
    """Drive the non-interactive discovery update up to its held reload task.

    The step claims the latch and reloads *directly*, in a task. This helper
    stops right after the task was handed to ``async_create_task``, which the
    stub keeps instead of running, so a test decides when the reload ends and
    observes the latch rather than racing it.

    ``reload_result`` is what the core hands back. ``loaded_components`` becomes
    ``hass.config.components``: it is the one externally visible difference
    between a falsy reload that already passed a release point and one that
    never reached our setup at all.

    Returns the hass stub, the entry, the integration package (for latch
    queries) and the step result.
    """

    entry = make_config_entry(
        entry_id="entry-id",
        data={
            CONF_GOOGLE_EMAIL: "existing@example.com",
            CONF_OAUTH_TOKEN: "old-token",
        },
        unique_id=unique_account_id("existing@example.com"),
        subentries={},
        state="loaded",
        source="user",
        disabled_by=None,
    )

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            self.updated: list[tuple[Any, dict[str, Any]]] = []
            self.reloaded: list[str] = []
            attach_config_entries_flow_manager(self)

        def async_entries(self, domain: str) -> list[Any]:
            return [entry]

        def async_update_entry(self, target: Any, **updates: Any) -> None:
            self.updated.append((target, updates))

        async def async_reload(self, entry_id: str) -> Any:
            self.reloaded.append(entry_id)
            return reload_result

        def async_get_entry(self, entry_id: str) -> Any | None:
            return entry if entry_id == entry.entry_id else None

        def async_get_subentries(self, entry_id: str) -> list[Any]:
            return []

        async def async_setup(self, entry_id: str) -> bool:
            return True

    class _Hass:
        def __init__(self) -> None:
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )
            self.data: dict[str, Any] = {}
            self.reload_coros: list[Any] = []
            # The shared helper reads the loaded components to tell the three
            # falsy causes apart, so this stub has to carry them.
            self.config = types.SimpleNamespace(components=set(loaded_components))

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            self.reload_coros.append(coro)
            return None

    async def _fake_ingest(
        flow: config_flow.ConfigFlow,
        normalized: Any,
        *,
        existing_entry: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return (
            {"data": {CONF_OAUTH_TOKEN: "unused"}},
            {CONF_OAUTH_TOKEN: "aas_et/UPDATED"},
        )

    monkeypatch.setattr(config_flow, "_ingest_discovery_credentials", _fake_ingest)
    monkeypatch.setattr(config_flow, "_find_entry_by_email", lambda _h, _e: entry)
    monkeypatch.setattr(
        config_flow,
        "_normalize_and_validate_discovery_payload",
        lambda _payload: config_flow.CloudDiscoveryData(
            email="existing@example.com",
            unique_id=unique_account_id("existing@example.com"),
            candidates=(("candidate", "aas_et/UPDATED"),),
            secrets_bundle=None,
        ),
    )

    hass = _Hass()
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._async_current_entries = types.MethodType(  # type: ignore[assignment]
        lambda self, *, include_ignore=False: [entry], flow
    )

    async def _set_unique_id(value: str, *, raise_on_progress: bool = False) -> None:
        set_config_flow_unique_id(flow, value)

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]

    result = await flow.async_step_discovery_update_info(
        {
            CONF_GOOGLE_EMAIL: "existing@example.com",
            "candidate_tokens": ["aas_et/UPDATED"],
        }
    )
    if inspect.isawaitable(result):
        result = await result

    integration = config_flow.import_integration_package()
    assert hass.reload_coros, "the flow did not reload directly at all"
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is False, (
        "the flow has to claim the latch before reloading directly"
    )

    return hass, entry, integration, result


@pytest.mark.asyncio
async def test_a_discovery_update_reload_that_never_set_up_gives_the_latch_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A falsy reload that never reached our setup is a dead end like a raise.

    ``async_reload`` returns ``entry.state is ConfigEntryState.LOADED`` from the
    ``entry.domain not in hass.config.components`` short circuit, so a component
    that could not be set up reports failure by *returning* falsy. Our
    ``async_setup_entry`` never ran, so the head discard never ran either, and
    neither did the unload ``finally``. The claim is a promise to reload; with no
    release point behind it the latch would swallow every later credential write
    for this entry until the process restarts.
    """

    hass, entry, integration, result = await _drive_discovery_update_reload(
        monkeypatch,
        reload_result=False,
        loaded_components=(),
    )

    with caplog.at_level("WARNING"):
        await hass.reload_coros[0]

    assert result["type"] == "abort"
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is True, (
        "the reload returned falsy without reaching a setup, so no release "
        "point ran and the latch has to be free again"
    )
    assert "stay ineffective" in caplog.text, (
        "a silently released latch hides that the written credentials are inert"
    )


@pytest.mark.asyncio
async def test_a_discovery_update_reload_that_failed_after_a_release_point_keeps_the_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every falsy reload is a dead end, and releasing anyway does damage.

    Of the three falsy causes two already passed a release point: a failed
    unload ran our ``async_unload_entry`` whose ``finally`` handed the latch
    back, and an entry setup that returned ``False`` ran our
    ``async_setup_entry`` whose head did the same. Both are indistinguishable
    from the outside except by one signal: the domain is still among the loaded
    components. Releasing here would discard a claim another writer may have
    taken in the meantime, which is the double reload this latch exists to
    prevent.

    This test is the guard against the simpler-looking rule "falsy means dead
    end", which an earlier draft of this change proposed.
    """

    hass, entry, integration, result = await _drive_discovery_update_reload(
        monkeypatch,
        reload_result=False,
        loaded_components=(config_flow.DOMAIN,),
    )

    await hass.reload_coros[0]

    assert result["type"] == "abort"
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is False, (
        "the component stayed loaded, so one of our lifecycle hooks ran and "
        "released the latch itself; releasing again could drop a fresh claim"
    )


@pytest.mark.asyncio
async def test_a_discovery_update_reload_that_succeeds_keeps_the_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload that landed releases the latch through unload and setup, not here.

    Releasing on success would leave the entry unlatched while its reload is
    still on its way, and the next claimant would queue a second teardown.
    """

    hass, entry, integration, result = await _drive_discovery_update_reload(
        monkeypatch,
        reload_result=True,
        loaded_components=(config_flow.DOMAIN,),
    )

    await hass.reload_coros[0]

    assert result["type"] == "abort"
    assert integration.claim_pending_entry_reload(hass, entry.entry_id) is False, (
        "the reload arrived; unload and setup release the latch themselves"
    )

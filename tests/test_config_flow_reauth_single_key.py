# tests/test_config_flow_reauth_single_key.py
"""Single-key (shared_key) enforcement for the reauth confirm flow.

The reauth flow (``async_step_reauth_confirm``) accepts a pasted ``secrets.json``
bundle just like the initial, options, and discovery import surfaces. Those
surfaces all gate on the single-key rule (a bundle is importable only if it
carries a usable ``shared_key``), but the reauth persist paths historically only
logged a warning and persisted anyway, bypassing the gate.

These tests pin the gate at *both* reauth persist sub-paths:

* the happy persist path (``async_update_reload_and_abort`` after token
  validation), and
* the multi-entry-guard *deferral* path (persist after an entry-scope guard
  error short-circuits validation).

A shared_key-less bundle must set ``errors["base"] == "keys_missing"`` and must
not persist, while a bundle that carries a ``shared_key`` must still persist.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
)
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
)

# A realistic 32-byte (64 hex chars) shared key value.
_SHARED_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"
_GUARD_MESSAGE = "Multiple config entries active for this integration"


class _ReauthEntry:
    """Minimal ConfigEntry substitute for the reauth flow under test."""

    def __init__(self, entry_id: str, email: str) -> None:
        self.entry_id = entry_id
        self.data: dict[str, Any] = {CONF_GOOGLE_EMAIL: email}
        self.options: dict[str, Any] = {}


def _build_reauth_flow(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pick_raises: BaseException | None = None,
    pick_returns_none: bool = False,
) -> tuple[Any, _ReauthEntry, dict[str, Any]]:
    """Wire a ``ConfigFlow`` instance for ``async_step_reauth_confirm`` tests.

    ``pick_raises`` lets a test drive the multi-entry-guard *deferral* path by
    making token validation raise an entry-scope guard error.
    ``pick_returns_none`` simulates a dead/expired token so the probe (if it is
    reached at all) fails. ``captured`` records any
    ``async_update_reload_and_abort`` persistence so tests can assert a blocked
    bundle never persists, and records every ``async_pick_working_token`` call
    so tests can assert the gate dominates the probe.
    """

    entry = _ReauthEntry("entry-reauth", "user@example.com")
    captured: dict[str, Any] = {}
    captured["pick_calls"] = 0

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        captured["pick_calls"] += 1
        if pick_raises is not None:
            raise pick_raises
        if pick_returns_none:
            return None
        return candidates[0][1] if candidates else None

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)

    class _ConfigEntries(ConfigEntriesDomainUniqueIdLookupMixin):
        def __init__(self) -> None:
            attach_config_entries_flow_manager(self)

        def async_get_entry(self, entry_id: str) -> _ReauthEntry | None:
            return entry if entry_id == entry.entry_id else None

        def async_entries(self, domain: str) -> list[Any]:
            return [entry]

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
    flow.context = {"entry_id": entry.entry_id}

    def _update_reload_and_abort(
        *, entry: Any, data: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        captured["persist"] = {"entry": entry, "data": data, "reason": reason}
        return {"type": "abort", "reason": reason}

    def _abort(*, reason: str) -> dict[str, Any]:
        captured["abort"] = reason
        return {"type": "abort", "reason": reason}

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    flow.async_update_reload_and_abort = _update_reload_and_abort  # type: ignore[assignment]
    flow.async_abort = _abort  # type: ignore[assignment]
    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]
    return flow, entry, captured


async def _run_reauth(flow: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Drive ``async_step_reauth_confirm`` with a pasted secrets bundle."""
    result = await flow.async_step_reauth_confirm(
        {config_flow._REAUTH_FIELD_SECRETS: json.dumps(payload)}
    )
    if inspect.isawaitable(result):
        result = await result
    assert isinstance(result, dict)
    return result


def _shared_missing_bundle() -> dict[str, Any]:
    """A valid-token, shared_key-less secrets bundle (the blocked input)."""
    return {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "owner_key": "AABBCC",
    }


def _shared_present_bundle() -> dict[str, Any]:
    """A valid-token bundle carrying a usable shared_key (the accepted input)."""
    return {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "shared_key": _SHARED_HEX,
    }


@pytest.mark.asyncio
async def test_reauth_happy_path_blocks_when_shared_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth happy path rejects a shared_key-less bundle and does not persist."""
    flow, _entry, captured = _build_reauth_flow(monkeypatch)

    result = await _run_reauth(flow, _shared_missing_bundle())

    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "keys_missing"}
    assert "persist" not in captured


@pytest.mark.asyncio
async def test_reauth_gate_precedes_probe_when_token_dead_and_shared_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single-key gate must precede the token probe on the reauth surface.

    Category: gate must precede token probe on every secrets surface. A
    shared_key-less bundle paired with a dead/expired token previously reached
    the form with ``cannot_connect`` (the token probe ran first and failed),
    masking the deterministic ``keys_missing`` the other import surfaces return.
    With the gate hoisted above the probe, the gate dominates: the result is
    ``keys_missing`` and ``async_pick_working_token`` is never called for this
    bundle.
    """
    flow, _entry, captured = _build_reauth_flow(monkeypatch, pick_returns_none=True)

    result = await _run_reauth(flow, _shared_missing_bundle())

    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "keys_missing"}
    assert "persist" not in captured
    # Gate dominates the probe: a shared_key-less bundle is rejected before any
    # token validation, so the (dead) token is never probed.
    assert captured["pick_calls"] == 0


@pytest.mark.asyncio
async def test_reauth_happy_path_persists_when_shared_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth happy path persists a bundle that carries a shared_key."""
    flow, _entry, captured = _build_reauth_flow(monkeypatch)

    await _run_reauth(flow, _shared_present_bundle())

    assert "persist" in captured
    persisted = captured["persist"]["data"]
    assert persisted[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    assert persisted[CONF_OAUTH_TOKEN] == "aas_et/FROM_SECRETS"


@pytest.mark.asyncio
async def test_reauth_deferral_path_blocks_when_shared_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reauth multi-entry-guard deferral path also gates on the shared_key.

    Token validation raises an entry-scope guard error, which routes the flow
    into the deferral persist branch. That branch must still reject a
    shared_key-less bundle instead of persisting it.
    """
    flow, _entry, captured = _build_reauth_flow(
        monkeypatch, pick_raises=RuntimeError(_GUARD_MESSAGE)
    )

    result = await _run_reauth(flow, _shared_missing_bundle())

    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "keys_missing"}
    assert "persist" not in captured


@pytest.mark.asyncio
async def test_reauth_deferral_path_persists_when_shared_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferral path still persists a bundle that carries a shared_key."""
    flow, _entry, captured = _build_reauth_flow(
        monkeypatch, pick_raises=RuntimeError(_GUARD_MESSAGE)
    )

    await _run_reauth(flow, _shared_present_bundle())

    assert "persist" in captured
    persisted = captured["persist"]["data"]
    assert persisted[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX


# ---------------------------------------------------------------------------
# Reauth normalization parity: the reauth surface must normalize (and
# scoped-key promote) the pasted bundle itself, so the gate, the credential
# extraction, and the persisted bundle all agree -- rather than relying on an
# implicit "already normalized" invariant from an upstream caller.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reauth_accepts_and_promotes_scoped_only_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped-only bundle is accepted on reauth and persisted promoted.

    The bundle carries only a scoped ``shared_key_<email>`` (no top-level
    ``shared_key``). The reauth surface must promote it to the canonical
    top-level slot so the single-key gate passes and the persisted bundle
    exposes the top-level ``shared_key`` the runtime reads first.
    """
    flow, _entry, captured = _build_reauth_flow(monkeypatch)

    scoped_only = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "shared_key_user@example.com": _SHARED_HEX,
    }

    await _run_reauth(flow, scoped_only)

    assert "persist" in captured, "scoped-only bundle must be accepted on reauth"
    persisted = captured["persist"]["data"]
    bundle = persisted[DATA_SECRET_BUNDLE]
    assert bundle["shared_key"] == _SHARED_HEX
    # The scoped entry is preserved alongside the promoted canonical key.
    assert bundle["shared_key_user@example.com"] == _SHARED_HEX


@pytest.mark.asyncio
async def test_reauth_persists_whitespace_free_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrapped-paste owner_key is persisted with all whitespace removed.

    A stray interior space in ``owner_key`` breaks AES-GCM decryption; the
    reauth persist path must apply the same credential normalization every other
    import surface uses.
    """
    flow, _entry, captured = _build_reauth_flow(monkeypatch)

    bundle_in = {
        "google_email": "user@example.com",
        "aas_token": "aas_et/FROM_SECRETS",
        "shared_key": _SHARED_HEX,
        "owner_key": "AA BB\nCC ",
    }

    await _run_reauth(flow, bundle_in)

    assert "persist" in captured
    persisted = captured["persist"]["data"]
    assert persisted[DATA_SECRET_BUNDLE]["owner_key"] == "AABBCC"

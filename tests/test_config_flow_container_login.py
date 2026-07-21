# tests/test_config_flow_container_login.py
"""Config-flow coverage for the one-click container-login path (Track B).

These tests exercise the container-login surfaces of
``custom_components.googlefindmy.config_flow`` -- initial setup
(``async_step_container_login``), reauth (``_async_reauth_container_persist``
via ``async_step_reauth_confirm``) and options
(``_async_options_container_persist``) -- without touching the network.

The two client primitives that talk to the login container,
``fetch_secrets_from_container`` and ``ack_consumed``, are imported *into* the
``config_flow`` module (``from .container_login import ...``), so the
*config-flow* tests monkeypatch them on ``config_flow`` itself.
``async_pick_working_token`` and ``async_get_clientsession`` are likewise
patched on the module so the shared validation pipeline runs against controlled
inputs.

Because that monkeypatching replaces the client wholesale, the config-flow
tests say nothing about the client itself. The final section of this module
therefore drives the **real** ``container_login`` primitives against a fake
``aiohttp`` session (canned status/content-type/body, no network), so the HTTP
contract is covered where it actually lives.

Covered (config-flow surfaces, client monkeypatched):

* Happy path initial setup: user step -> ``container_login`` auth method ->
  fetch returns a valid bundle -> device_selection, with the persisted auth data
  carrying the validated token. Also pins the form defaults: ``port`` default is
  ``CONTAINER_TOKEN_PORT`` (7901) and the ``novnc_url`` placeholder targets the
  noVNC port (``:7900``).
* Two-phase-delete timing (F4): ``container_login`` STAGES the ack instead of
  sending it; the ack fires only when the entry is actually created (the
  ``_async_flush_container_ack`` helper that ``device_selection`` calls right
  after ``async_create_entry``). An abort before that flush keeps the bundle and
  sends no ack.
* Happy path reauth and options container branches (fetch mocked, persist +
  ack observed).
* Error mapping: ``ContainerUnreachableError`` -> ``container_unreachable``;
  ``ContainerTimeoutError`` -> ``container_timeout``; ``ContainerAuthError`` ->
  ``container_auth_failed``; a shared_key-less bundle -> the existing
  ``keys_missing`` gate; an empty pairing code -> ``required``.
* Security negatives: a wrong pairing code surfaces as ``ContainerAuthError``;
  no token/bundle content ever reaches the HA log (only shape patterns like
  ``chars=`` / type names); and the two-phase-delete contract -- when
  ``async_pick_working_token`` fails, ``ack_consumed`` is NOT called, so the
  container keeps the on-disk secret until its TTL.

Covered (real ``container_login`` client, fake session, F-A2/F-N3):

* Timeout mapping: the builtin ``TimeoutError`` raised by aiohttp on a total
  timeout becomes ``ContainerTimeoutError`` on both paths.
* Status-mapping asymmetry for HTTP 410 -- the invariant a future refactor of
  ``gone_is_auth`` would break: on the *fetch* path 410 is a
  ``ContainerAuthError`` (code expired/locked out, or the one-shot bundle was
  already collected) and additionally sets ``code_used=True`` so the config
  flow can offer "restart the container" instead of "check your code"; on the
  *ack* path 410 stays a **success** ("already gone", the two-phase delete
  landed), and 401/403 are auth failures with ``code_used=False`` on both.
* Fetch response validation: refused redirect (3xx) and other non-auth statuses
  -> ``ContainerUnreachableError``; wrong content type, a body exceeding
  ``CONTAINER_MAX_RESPONSE_BYTES``, non-JSON and structurally malformed
  payloads (no object, missing/empty ``bundle``/``delete_token``) likewise;
  the happy path returns ``(bundle, delete_token)`` verbatim.
* ACK status handling: 200 and 410 succeed, any other status and any
  ``aiohttp.ClientError`` raise ``ContainerUnreachableError``.
* SSRF best-effort guard: a literal IPv4 link-local/metadata host
  (``169.254.169.254``) is rejected on both paths *before* any request is
  issued (the fake session records zero calls).

Conventions (tests/AGENTS.md): ``make_config_entry`` for config-entry doubles,
``pytestmark = pytest.mark.asyncio``, no ``asyncio.run``, no ``pathspec``
import, ``aiohttp`` allowed (imported for its error types; the sessions used
here are local fakes, never a real client session).
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

import aiohttp
import pytest
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow, container_login
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    CONTAINER_MAX_RESPONSE_BYTES,
    CONTAINER_NONCE_MIN_LEN,
    CONTAINER_NOVNC_PORT,
    CONTAINER_TOKEN_PORT,
    DATA_SECRET_BUNDLE,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.config_flow import (
    ConfigEntriesDomainUniqueIdLookupMixin,
    attach_config_entries_flow_manager,
    prepare_flow_hass_config_entries,
)

pytestmark = pytest.mark.asyncio

# A realistic 32-byte (64 hex chars) shared key value.
_SHARED_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"
_EMAIL = "user@example.com"
_TOKEN = "aas_et/FROM_CONTAINER"
_DELETE_TOKEN = "delete-token-xyz"
_PAIRING_CODE = "pairing-code-abcdef0123456789"


def _valid_bundle() -> dict[str, Any]:
    """A container bundle that carries a usable shared_key and an aas token."""

    return {
        "google_email": _EMAIL,
        "aas_token": _TOKEN,
        "shared_key": _SHARED_HEX,
    }


def _shared_missing_bundle() -> dict[str, Any]:
    """A valid-token bundle WITHOUT a shared_key (blocked by the keys gate)."""

    return {
        "google_email": _EMAIL,
        "aas_token": _TOKEN,
        "owner_key": "AABBCC",
    }


class _Recorder:
    """Collects fetch/ack calls so tests can assert the two-phase-delete order."""

    def __init__(self) -> None:
        self.fetch_calls: list[dict[str, Any]] = []
        self.ack_calls: list[dict[str, Any]] = []
        self.pick_calls = 0


def _install_container_client(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    fetch_raises: BaseException | None = None,
    bundle: dict[str, Any] | None = None,
    pick_returns_none: bool = False,
    pick_raises: BaseException | None = None,
) -> None:
    """Patch the container client + token probe on the ``config_flow`` module.

    ``fetch_secrets_from_container`` returns ``(bundle, delete_token)`` or raises
    the provided container error. ``ack_consumed`` merely records that it ran.
    ``async_pick_working_token`` returns the first candidate token unless a test
    forces a failure (``pick_returns_none`` / ``pick_raises``).
    """

    resolved_bundle = _valid_bundle() if bundle is None else bundle

    async def _fake_fetch(
        session: Any,
        host: str,
        port: int,
        nonce: str,
        *,
        timeout: float,
    ) -> tuple[dict[str, Any], str]:
        recorder.fetch_calls.append(
            {"host": host, "port": port, "nonce": nonce, "timeout": timeout}
        )
        if fetch_raises is not None:
            raise fetch_raises
        return dict(resolved_bundle), _DELETE_TOKEN

    async def _fake_ack(
        session: Any,
        host: str,
        port: int,
        nonce: str,
        delete_token: str,
        *,
        timeout: float,
    ) -> None:
        recorder.ack_calls.append(
            {"host": host, "port": port, "delete_token": delete_token}
        )

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        recorder.pick_calls += 1
        if pick_raises is not None:
            raise pick_raises
        if pick_returns_none:
            return None
        return candidates[0][1] if candidates else None

    monkeypatch.setattr(config_flow, "fetch_secrets_from_container", _fake_fetch)
    monkeypatch.setattr(config_flow, "ack_consumed", _fake_ack)
    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)
    # The flow calls async_get_clientsession(self.hass); return a sentinel that
    # is never actually used because the client is fully mocked above.
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda hass: object())


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
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

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


# ---------------------------------------------------------------------------
# Initial setup: user step routing + happy path
# ---------------------------------------------------------------------------


async def test_user_step_routes_container_method_to_container_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing the container auth method routes to the container-login form."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    # No user_input -> the container step shows its form with the port/noVNC hints.
    result = await _maybe_await(
        flow.async_step_user({"auth_method": config_flow._AUTH_METHOD_CONTAINER})
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("step_id") == "container_login"


async def test_container_form_defaults_port_and_novnc_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial container form defaults port to 7901 and links noVNC on :7900."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(flow.async_step_container_login(None))
    assert isinstance(result, dict)
    assert result.get("type") == "form"

    # Port default == CONTAINER_TOKEN_PORT (7901).
    assert CONTAINER_TOKEN_PORT == 7901
    schema = result["data_schema"].schema
    port_default = next(
        marker.default()
        for marker in schema
        if getattr(marker, "schema", None) == "port"
    )
    assert port_default == CONTAINER_TOKEN_PORT == 7901

    # The noVNC placeholder targets the noVNC port, not the token port.
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("novnc_url") == f"http://127.0.0.1:{CONTAINER_NOVNC_PORT}"
    assert placeholders["novnc_url"].endswith(":7900")
    assert CONTAINER_NOVNC_PORT == 7900


async def test_initial_setup_happy_path_persists_token_and_defers_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid container fetch persists the token and DEFERS the ack (F4).

    The two-phase-delete ack must not fire in ``container_login``: it is staged
    and only sent once ``device_selection`` actually creates the config entry.
    Here ``device_selection`` is stubbed (no entry created), so after the step
    the fetch has run, the result is staged, and NO ack has been sent yet.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    captured: dict[str, Any] = {}

    async def _fake_device_selection() -> dict[str, Any]:
        captured["reached_device_selection"] = True
        captured["auth_data"] = dict(flow._auth_data)
        # Snapshot the staged (pending) ack at the moment device_selection runs.
        captured["pending_before_create"] = flow._container_pending_ack
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert captured.get("reached_device_selection") is True

    # The validated token + email were staged for persistence.
    auth_data = captured["auth_data"]
    assert auth_data[CONF_OAUTH_TOKEN] == _TOKEN
    assert auth_data[CONF_GOOGLE_EMAIL] == _EMAIL
    assert auth_data[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX

    # Two-phase delete (F4): the fetch ran, but the ack is DEFERRED. A pending
    # ack result is staged and no ack has been sent while the entry is not yet
    # created.
    assert len(recorder.fetch_calls) == 1
    assert recorder.fetch_calls[0]["nonce"] == _PAIRING_CODE
    assert captured["pending_before_create"] is not None
    assert captured["pending_before_create"].delete_token == _DELETE_TOKEN
    assert recorder.ack_calls == []


async def test_pending_ack_flushes_once_entry_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred ack fires exactly once when the entry is actually created (F4).

    Drives ``container_login`` to stage the pending ack, then invokes the flush
    helper that ``device_selection`` calls right after ``async_create_entry``.
    The ack must be sent once and the pending slot cleared (no double-ack).
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    async def _fake_device_selection() -> dict[str, Any]:
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    # Not acked while the entry is not yet created.
    assert recorder.ack_calls == []
    assert flow._container_pending_ack is not None

    # Simulate device_selection reaching CREATE_ENTRY and flushing the ack.
    await flow._async_flush_container_ack()
    assert len(recorder.ack_calls) == 1
    assert recorder.ack_calls[0]["delete_token"] == _DELETE_TOKEN
    # Cleared: a second flush is a no-op (no double-ack).
    assert flow._container_pending_ack is None
    await flow._async_flush_container_ack()
    assert len(recorder.ack_calls) == 1


async def test_aborted_flow_before_entry_keeps_bundle_no_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user aborts before the entry is created, no ack is sent (F4).

    ``container_login`` stages the pending ack; if the flow never reaches
    CREATE_ENTRY (the flush helper is never invoked), the ack must not fire, so
    the container keeps its on-disk secret for a retry (TTL fallback).
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    async def _fake_device_selection() -> dict[str, Any]:
        # User is shown a form and abandons the flow: no create, no flush.
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )

    # The credential is staged but the container was NOT told to delete it.
    assert flow._container_pending_ack is not None
    assert recorder.ack_calls == []


# ---------------------------------------------------------------------------
# Initial setup: error paths
# ---------------------------------------------------------------------------


async def test_empty_pairing_code_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty pairing code short-circuits with a ``required`` field error."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(
        flow.async_step_container_login(
            {"host": "127.0.0.1", "port": CONTAINER_TOKEN_PORT, "pairing_code": "   "}
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"pairing_code": "required"}
    # No network call was attempted.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []


@pytest.mark.parametrize(
    ("exc_factory", "expected_key"),
    [
        (
            lambda: config_flow.ContainerUnreachableError("boom"),
            "container_unreachable",
        ),
        (lambda: config_flow.ContainerTimeoutError("slow"), "container_timeout"),
        (lambda: config_flow.ContainerAuthError("nope"), "container_auth_failed"),
        # A *spent* code is not a wrong code: the token server is one-shot, so a
        # retry after a successful fetch hits HTTP 410. Telling the user to check
        # the code would be wrong; the remedy is restarting the login container.
        (
            lambda: config_flow.ContainerAuthError("spent", code_used=True),
            "container_code_used",
        ),
    ],
)
async def test_container_fetch_errors_map_to_keys(
    monkeypatch: pytest.MonkeyPatch,
    exc_factory: Any,
    expected_key: str,
) -> None:
    """Each typed container error maps to its dedicated HA error key; no ack runs."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder, fetch_raises=exc_factory())
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": expected_key}
    # A failed fetch never triggers the second-phase delete.
    assert recorder.ack_calls == []


async def test_too_short_pairing_code_is_rejected_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code below CONTAINER_NONCE_MIN_LEN fails the form, without a round-trip.

    The constant promises rejection "before any network round-trip", so this
    pins both halves: the dedicated field error *and* the absence of a fetch.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    short_code = "x" * (CONTAINER_NONCE_MIN_LEN - 1)
    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": short_code,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"pairing_code": "container_code_too_short"}
    # The whole point of the gate: no request left the process.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []


async def test_shared_key_missing_bundle_hits_keys_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared_key-less container bundle is blocked by the existing keys gate."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder, bundle=_shared_missing_bundle())
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "keys_missing"}
    # The keys gate precedes the token probe and the ack.
    assert recorder.pick_calls == 0
    assert recorder.ack_calls == []


# ---------------------------------------------------------------------------
# Security negatives
# ---------------------------------------------------------------------------


async def test_wrong_pairing_code_surfaces_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong pairing code (fetch raises ContainerAuthError) -> container_auth_failed."""

    recorder = _Recorder()
    _install_container_client(
        monkeypatch,
        recorder,
        fetch_raises=config_flow.ContainerAuthError("wrong code"),
    )
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    # The code must be long enough to clear the client-side length gate
    # (CONTAINER_NONCE_MIN_LEN); this test is about a *rejected* code, not a
    # malformed one, so the rejection has to come from the fetch call.
    wrong_but_well_formed = "wrong-code-0123456789abcdef"
    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": wrong_but_well_formed,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("errors") == {"base": "container_auth_failed"}
    assert recorder.ack_calls == []


async def test_no_token_or_bundle_content_in_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The container-login path never logs bundle/token content, only shapes."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    async def _fake_device_selection() -> dict[str, Any]:
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        await _maybe_await(
            flow.async_step_container_login(
                {
                    "host": "127.0.0.1",
                    "port": CONTAINER_TOKEN_PORT,
                    "pairing_code": _PAIRING_CODE,
                }
            )
        )

    log_text = caplog.text
    # Neither the pairing nonce, the delete token, the aas token, nor the shared
    # key may appear anywhere in the captured log output.
    assert _PAIRING_CODE not in log_text
    assert _DELETE_TOKEN not in log_text
    assert _TOKEN not in log_text
    assert _SHARED_HEX not in log_text


async def test_ack_not_called_when_token_selection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-phase-delete safety: a failed token pick must NOT trigger ``ack_consumed``.

    If ``async_pick_working_token`` cannot validate any candidate, the flow
    records an error and returns without persisting; the second-phase
    ``ack_consumed`` MUST be skipped so the container keeps the on-disk secret
    until its TTL fallback (the credential is not lost on a HA-side failure).
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder, pick_returns_none=True)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(
        flow.async_step_container_login(
            {
                "host": "127.0.0.1",
                "port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "cannot_connect"}
    # The fetch ran and the token probe ran, but the ack did NOT.
    assert len(recorder.fetch_calls) == 1
    assert recorder.pick_calls == 1
    assert recorder.ack_calls == []


# ---------------------------------------------------------------------------
# Reauth container branch
# ---------------------------------------------------------------------------


async def test_reauth_container_branch_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reauth container branch fetches, persists, and acks for the bound email."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reauth",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}

    captured: dict[str, Any] = {}

    def _update_reload_and_abort(
        *, entry: Any, data: dict[str, Any], reason: str, **_: Any
    ) -> dict[str, Any]:
        captured["data"] = data
        captured["reason"] = reason
        return {"type": "abort", "reason": reason}

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    flow.async_update_reload_and_abort = _update_reload_and_abort  # type: ignore[assignment]
    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]

    result = await _maybe_await(
        flow.async_step_reauth_confirm(
            {
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    # Persisted the validated bundle and acked the container.
    assert captured["data"][DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    assert captured["data"][CONF_OAUTH_TOKEN] == _TOKEN
    assert len(recorder.ack_calls) == 1


async def test_reauth_container_branch_error_does_not_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reauth container fetch failure re-shows the form and never acks."""

    recorder = _Recorder()
    _install_container_client(
        monkeypatch,
        recorder,
        fetch_raises=config_flow.ContainerTimeoutError("slow"),
    )

    entry = make_config_entry(
        entry_id="entry-reauth",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]

    result = await _maybe_await(
        flow.async_step_reauth_confirm(
            {
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "container_timeout"}
    assert recorder.ack_calls == []


# ---------------------------------------------------------------------------
# Options container branch
# ---------------------------------------------------------------------------


async def test_options_container_branch_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options container branch persists via async_update_entry and acks."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-options",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    # Options flows carry the bound config entry.
    flow.config_entry = entry  # type: ignore[attr-defined]

    captured: dict[str, Any] = {}

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    async def _refresh_title(_entry: Any, _opt: Any) -> None:
        return None

    def _abort(*, reason: str, **_: Any) -> dict[str, Any]:
        captured["reason"] = reason
        return {"type": "abort", "reason": reason}

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]
    flow._async_refresh_subentry_entry_title = _refresh_title  # type: ignore[attr-defined]
    flow.async_abort = _abort  # type: ignore[assignment]

    errors: dict[str, str] = {}
    result = await flow._async_options_container_persist(
        entry=entry,
        selected_option=None,
        user_input={
            "container_host": "127.0.0.1",
            "container_port": CONTAINER_TOKEN_PORT,
            "pairing_code": _PAIRING_CODE,
        },
        errors=errors,
    )
    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert result.get("reason") == "reconfigure_successful"
    assert errors == {}
    # Persisted the validated bundle onto the entry and acked the container.
    assert entry.data[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    assert entry.data[CONF_OAUTH_TOKEN] == _TOKEN
    assert len(recorder.ack_calls) == 1


async def test_options_container_branch_error_does_not_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An options container fetch failure records an error and never acks."""

    recorder = _Recorder()
    _install_container_client(
        monkeypatch,
        recorder,
        fetch_raises=config_flow.ContainerUnreachableError("down"),
    )

    entry = make_config_entry(
        entry_id="entry-options",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]

    errors: dict[str, str] = {}
    result = await flow._async_options_container_persist(
        entry=entry,
        selected_option=None,
        user_input={
            "container_host": "127.0.0.1",
            "container_port": CONTAINER_TOKEN_PORT,
            "pairing_code": _PAIRING_CODE,
        },
        errors=errors,
    )
    assert result is None
    assert errors == {"base": "container_unreachable"}
    assert recorder.ack_calls == []


async def test_options_handler_inherits_container_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F-N4: OptionsFlowHandler must inherit the shared container fetch/ack helpers.

    The options credential-refresh path calls ``_async_container_fetch`` and
    ``_async_container_ack`` (both extracted to ``_ContainerLoginMixin``). If they
    were reachable only on ``ConfigFlow`` the options persist would raise
    ``AttributeError`` before any request. This asserts both are bound callables
    on the handler and that a full ``_async_options_container_persist`` run does
    not raise ``AttributeError``.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-inherit",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    # The shared helpers are inherited from _ContainerLoginMixin.
    assert callable(getattr(flow, "_async_container_fetch", None))
    assert callable(getattr(flow, "_async_container_ack", None))

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    async def _refresh_title(_entry: Any, _opt: Any) -> None:
        return None

    def _abort(*, reason: str, **_: Any) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]
    flow._async_refresh_subentry_entry_title = _refresh_title  # type: ignore[attr-defined]
    flow.async_abort = _abort  # type: ignore[assignment]

    errors: dict[str, str] = {}
    # Must not raise AttributeError (the F-N4 regression).
    result = await flow._async_options_container_persist(
        entry=entry,
        selected_option=None,
        user_input={
            "container_host": "127.0.0.1",
            "container_port": CONTAINER_TOKEN_PORT,
            "pairing_code": _PAIRING_CODE,
        },
        errors=errors,
    )
    assert isinstance(result, dict)
    assert result.get("reason") == "reconfigure_successful"
    # fetch validated the bundle and the ack fired after persist (order: fetch ->
    # update_entry -> ack), so exactly one of each ran.
    assert len(recorder.fetch_calls) == 1
    assert len(recorder.ack_calls) == 1


# --- F-N3: builtin TimeoutError (total timeout) mapping -----------------------


class _TimeoutCtx:
    """Async context manager whose __aenter__ raises the builtin TimeoutError.

    aiohttp raises the builtin ``TimeoutError`` on a total (``ClientTimeout``)
    timeout; it is neither ``aiohttp.ServerTimeoutError`` nor an
    ``aiohttp.ClientError`` subclass, so the client primitives must catch it
    explicitly and translate it into ``ContainerTimeoutError``.
    """

    async def __aenter__(self) -> Any:
        raise TimeoutError("total timeout")

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _TimeoutSession:
    """Fake aiohttp session whose get/post time out with the builtin error."""

    def get(self, *_args: Any, **_kwargs: Any) -> _TimeoutCtx:
        return _TimeoutCtx()

    def post(self, *_args: Any, **_kwargs: Any) -> _TimeoutCtx:
        return _TimeoutCtx()


async def test_fetch_maps_builtin_timeout_to_container_timeout() -> None:
    """F-N3: a total-timeout builtin ``TimeoutError`` maps in the fetch path."""

    with pytest.raises(container_login.ContainerTimeoutError):
        await container_login.fetch_secrets_from_container(
            _TimeoutSession(),  # type: ignore[arg-type]
            "localhost",
            CONTAINER_TOKEN_PORT,
            "nonce-value",
            timeout=1.0,
        )


async def test_ack_maps_builtin_timeout_to_container_timeout() -> None:
    """F-N3: a total-timeout builtin ``TimeoutError`` maps in the ACK path."""

    with pytest.raises(container_login.ContainerTimeoutError):
        await container_login.ack_consumed(
            _TimeoutSession(),  # type: ignore[arg-type]
            "localhost",
            CONTAINER_TOKEN_PORT,
            "nonce-value",
            _DELETE_TOKEN,
            timeout=1.0,
        )


# --- F-A2: the REAL client against a fake session ----------------------------
#
# Everything above this line monkeypatches ``fetch_secrets_from_container`` /
# ``ack_consumed`` away on ``config_flow`` and therefore proves nothing about
# the client. The tests below call the real primitives with a fake aiohttp
# session so the HTTP contract (status mapping, content type, size cap,
# link-local guard) is exercised where it is implemented.


class _FakeContent:
    """Stand-in for ``response.content`` honoring the caller's read limit."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int = -1) -> bytes:
        """Return at most ``limit`` bytes, like ``StreamReader.read``."""

        if limit < 0:
            return self._body
        return self._body[:limit]


class _FakeResponse:
    """Canned aiohttp response: status, content type and body only."""

    def __init__(
        self,
        status: int,
        *,
        body: bytes = b"{}",
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.content = _FakeContent(body)


class _FakeCtx:
    """Async context manager yielding a canned response (or raising)."""

    def __init__(
        self, response: _FakeResponse | None, error: BaseException | None
    ) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _FakeSession:
    """Fake aiohttp session recording every request it is asked to make.

    ``calls`` is what proves the SSRF guard runs *before* the request: a
    rejected host must leave the recorder empty.
    """

    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeCtx:
        """Record a GET and hand back the canned response."""

        self.calls.append(("GET", url, kwargs))
        return _FakeCtx(self.response, self.error)

    def post(self, url: str, **kwargs: Any) -> _FakeCtx:
        """Record a POST and hand back the canned response."""

        self.calls.append(("POST", url, kwargs))
        return _FakeCtx(self.response, self.error)


def _json_response(payload: Any, *, status: int = 200) -> _FakeResponse:
    """Build a JSON ``_FakeResponse`` from a payload object."""

    return _FakeResponse(status, body=json.dumps(payload).encode())


async def _fetch(session: _FakeSession, *, host: str = "127.0.0.1") -> Any:
    """Call the real fetch primitive with the fake session."""

    return await container_login.fetch_secrets_from_container(
        session,  # type: ignore[arg-type]
        host,
        CONTAINER_TOKEN_PORT,
        _PAIRING_CODE,
        timeout=1.0,
    )


async def _ack(session: _FakeSession, *, host: str = "127.0.0.1") -> None:
    """Call the real ack primitive with the fake session."""

    await container_login.ack_consumed(
        session,  # type: ignore[arg-type]
        host,
        CONTAINER_TOKEN_PORT,
        _PAIRING_CODE,
        _DELETE_TOKEN,
        timeout=1.0,
    )


async def test_fetch_410_is_auth_error_and_flags_code_used() -> None:
    """410 on the fetch path: auth error (never "unreachable") + ``code_used``.

    The endpoint is one-shot: 410 means the code expired, was locked out, or
    the bundle was already collected. Mapping it to
    ``ContainerUnreachableError`` would blame the host/port instead. The
    ``code_used`` flag lets the config flow say "restart the container" rather
    than "check your pairing code".
    """

    session = _FakeSession(_FakeResponse(410))

    with pytest.raises(container_login.ContainerAuthError) as excinfo:
        await _fetch(session)

    assert excinfo.value.code_used is True
    assert "410" in str(excinfo.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_401_403_are_auth_errors_without_code_used(status: int) -> None:
    """401/403 mean the code itself was wrong: auth error, ``code_used`` False."""

    session = _FakeSession(_FakeResponse(status))

    with pytest.raises(container_login.ContainerAuthError) as excinfo:
        await _fetch(session)

    assert excinfo.value.code_used is False
    assert str(status) in str(excinfo.value)


async def test_container_auth_error_defaults_code_used_to_false() -> None:
    """The single-argument construction stays valid (backwards compatible)."""

    err = container_login.ContainerAuthError("nope")

    assert err.code_used is False
    assert str(err) == "nope"


@pytest.mark.parametrize("status", [200, 410])
async def test_ack_treats_200_and_410_as_success(status: int) -> None:
    """ACK: 410 ("already gone") is the two-phase-delete success, like 200.

    This is the other half of the 410 asymmetry: inverting ``gone_is_auth``
    here would turn a completed delete into a spurious auth failure.
    """

    session = _FakeSession(_FakeResponse(status))

    await _ack(session)

    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/ack")
    # No redirect may ever be followed (Authorization header leak, OWASP A10).
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status", [401, 403])
async def test_ack_still_raises_auth_error_for_401_403(status: int) -> None:
    """The ack path keeps 401/403 as auth failures; only 410 is exempt."""

    session = _FakeSession(_FakeResponse(status))

    with pytest.raises(container_login.ContainerAuthError) as excinfo:
        await _ack(session)

    assert excinfo.value.code_used is False


async def test_ack_500_maps_to_unreachable() -> None:
    """Any other ack status is an unexpected reply -> unreachable."""

    session = _FakeSession(_FakeResponse(500))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _ack(session)

    assert "500" in str(excinfo.value)


async def test_ack_client_error_maps_to_unreachable() -> None:
    """A transport-level ``aiohttp.ClientError`` surfaces as unreachable."""

    session = _FakeSession(error=aiohttp.ClientError("connection refused"))

    with pytest.raises(container_login.ContainerUnreachableError):
        await _ack(session)


async def test_fetch_client_error_maps_to_unreachable() -> None:
    """Same transport mapping on the fetch path."""

    session = _FakeSession(error=aiohttp.ClientError("connection refused"))

    with pytest.raises(container_login.ContainerUnreachableError):
        await _fetch(session)


@pytest.mark.parametrize("status", [302, 500])
async def test_fetch_redirect_and_server_error_map_to_unreachable(
    status: int,
) -> None:
    """A refused redirect (3xx) and a 5xx are both "answered unexpectedly"."""

    session = _FakeSession(_FakeResponse(status))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert str(status) in str(excinfo.value)
    # The redirect was never followed: exactly one request left the client.
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False


async def test_fetch_rejects_wrong_content_type() -> None:
    """Only ``application/json`` is parsed; anything else is refused."""

    session = _FakeSession(
        _FakeResponse(200, body=b"<html>hi</html>", content_type="text/html")
    )

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert "text/html" in str(excinfo.value)


async def test_fetch_rejects_body_above_size_cap() -> None:
    """A body over ``CONTAINER_MAX_RESPONSE_BYTES`` is refused, not buffered."""

    oversized = b"x" * (CONTAINER_MAX_RESPONSE_BYTES + 1)
    session = _FakeSession(_FakeResponse(200, body=oversized))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert str(CONTAINER_MAX_RESPONSE_BYTES) in str(excinfo.value)


async def test_fetch_rejects_non_json_body() -> None:
    """A JSON content type with a broken body is refused, not propagated."""

    session = _FakeSession(_FakeResponse(200, body=b"{not json"))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert "not valid JSON" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"delete_token": _DELETE_TOKEN},
        {"bundle": {"aas_token": _TOKEN}},
        {"bundle": {"aas_token": _TOKEN}, "delete_token": ""},
        {"bundle": "not-a-dict", "delete_token": _DELETE_TOKEN},
    ],
)
async def test_fetch_rejects_malformed_payloads(payload: Any) -> None:
    """A structurally wrong payload never reaches the caller as a bundle."""

    session = _FakeSession(_json_response(payload))

    with pytest.raises(container_login.ContainerUnreachableError):
        await _fetch(session)


async def test_fetch_returns_bundle_and_delete_token_verbatim() -> None:
    """The happy path hands the bundle through unvalidated, plus the token."""

    bundle = _valid_bundle()
    session = _FakeSession(
        _json_response({"bundle": bundle, "delete_token": _DELETE_TOKEN})
    )

    result = await _fetch(session)

    assert result == (bundle, _DELETE_TOKEN)
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == f"http://127.0.0.1:{CONTAINER_TOKEN_PORT}/secrets"
    assert kwargs["headers"]["Authorization"] == f"Bearer {_PAIRING_CODE}"
    assert kwargs["allow_redirects"] is False


async def test_fetch_blocks_link_local_metadata_host_before_any_request() -> None:
    """The IPv4 metadata address is refused *before* a request is issued."""

    session = _FakeSession(_json_response({"bundle": {}, "delete_token": "x"}))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session, host="169.254.169.254")

    assert "link-local" in str(excinfo.value)
    assert session.calls == []


async def test_ack_blocks_link_local_metadata_host_before_any_request() -> None:
    """Same guard on the ack path (the delete token must not leak either)."""

    session = _FakeSession(_FakeResponse(200))

    with pytest.raises(container_login.ContainerUnreachableError):
        await _ack(session, host="169.254.169.254")

    assert session.calls == []


async def test_hostname_and_non_link_local_ip_are_not_blocked() -> None:
    """The guard is literal-IPv4-only: hostnames and normal IPs pass through.

    Pins the honest scope documented in the module docstring -- a blanket
    RFC1918 ban would break the ``127.0.0.1`` main case.
    """

    for host in ("localhost", "10.0.0.5", "169.253.1.1"):
        session = _FakeSession(
            _json_response({"bundle": _valid_bundle(), "delete_token": _DELETE_TOKEN})
        )
        await _fetch(session, host=host)
        assert len(session.calls) == 1

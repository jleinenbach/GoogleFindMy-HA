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
  ``CONTAINER_TOKEN_PORT`` (7901) and the ``novnc_access`` placeholder targets
  the noVNC port (``:7900``).
* noVNC link rendering: ``_classify_novnc_host`` /
  ``_novnc_access_placeholder`` only produce a clickable markdown link for a
  non-loopback IP literal. A loopback address or a hostname (``localhost``, the
  compose service name) is rendered as inline code, because the URL is opened
  by the operator's browser, which usually runs on a different machine than the
  Docker host and therefore cannot follow either of those.
* Credential-method exclusivity on reauth and options: a submission carrying
  more than one method (pasted ``secrets_json`` *and* a container
  ``pairing_code``) is rejected with ``choose_one`` BEFORE any network call, so
  the one-shot container fetch neither burns the pairing code nor silently
  discards the pasted bundle. Exactly one method still routes to its own path.
* Two-phase-delete timing (F4): ``container_login`` STAGES the ack instead of
  sending it; an abort before the entry exists keeps the bundle and sends no
  ack.
* Two-phase-delete timing, second hop (P2): reaching ``async_create_entry`` is
  still NOT the persist point -- it only builds a FlowResult, and Home
  Assistant creates and stores the entry afterwards in
  ``ConfigEntriesFlowManager.async_finish_flow``. ``device_selection``
  therefore hands the ack over to ``async_setup_entry`` through
  ``hass.data[DOMAIN]["pending_container_cleanup"]`` (in-memory only, never HA
  storage, and stripped of the credential payload). The runner sends it exactly
  once, ignores jobs staged for other accounts, and a reload does not re-ack.
  The inline flush of the REAL persist points (reconfigure/options, where
  ``async_update_entry`` writes through synchronously) is unchanged and pinned
  by regression tests that drive those production branches, not the helpers.
* Ack log-level grading: because the deferred ack only fires at the end of
  ``async_setup_entry``, the container's ``CONTAINER_TOKEN_TTL`` regularly wins
  the race and deletes the same file first. An unreachable container *after*
  that TTL is therefore logged at debug; anything else keeps its warning.
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
  *ack* path 410 is decided by the **body**, and 401/403 are auth failures with
  ``code_used=False`` on both.
* ACK outcome classification (F-C2): the deletion is confirmed by the response
  *body* (``{"status": "deleted"}`` on 200, ``{"status": "already_deleted"}`` on
  410), never by the status alone. The server sends that same 410 with
  ``{"error": "locked"}`` after a five-attempt lockout and then deliberately
  *keeps* ``secrets.json``, so that case raises (``ContainerAuthError``), as
  does any body that cannot be classified, on either accepted status
  (``ContainerUnreachableError``). Pinned end-to-end through
  ``_async_execute_container_cleanup`` with the real client: the lockout must
  reach the log as a warning, the idempotent 410 must stay silent.
* The retention discriminator: only the lockout body sets
  ``ContainerLoginError.secret_retained``, and only that flag (never the error
  *class*) may trigger the "the container kept its secrets.json, delete it
  manually" warning. A plain ``403`` -- the ack of a restarted container, whose
  nonce no longer matches -- is the same class with the opposite fact and keeps
  the generic TTL-fallback wording. Driven through the real client on both
  sides, plus the conservative ``False`` default on every error class.
* Chunked responses (F-C1): the body is drained to EOF, so a reply split across
  several TCP chunks is reassembled instead of being parsed half-read, while the
  ``CONTAINER_MAX_RESPONSE_BYTES`` ceiling (inclusive boundary) and the
  buffering bound still hold. Pinned end-to-end through
  ``async_step_container_login`` with the real client.
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
* Entry-removal drain (``async_discard_pending_container_cleanup_for_entry``,
  helper ``_stage_entry_ticket``): addressed by entry id only, so a concurrent
  same-account flow's uncorrelated ticket survives, and unbounded, so every
  ticket of the removed entry goes in one pass. Also pins the job-count return
  contract, the ``None`` guard and the empty-staging-area path.

Conventions (tests/AGENTS.md): ``make_config_entry`` for config-entry doubles,
``pytestmark = pytest.mark.asyncio``, no ``asyncio.run``, no ``pathspec``
import, ``aiohttp`` allowed (imported for its error types; the sessions used
here are local fakes, never a real client session).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from collections.abc import Mapping
from pathlib import Path
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
    CONTAINER_TOKEN_TTL,
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
            # ``hass.data`` is canonical on HomeAssistant and is where the flow
            # stages its deferred cleanup jobs, so the double must carry it.
            self.data: dict[str, Any] = {}
            prepare_flow_hass_config_entries(
                self,
                _ConfigEntries,
                frame_module=frame,
            )

        async def async_add_executor_job(
            self, func: Any, *args: Any, **kwargs: Any
        ) -> Any:
            # ``async_step_user`` reads the watched secrets paths through the
            # executor; a double without this method would silently skip that
            # scan instead of exercising it.
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

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


async def _drive_container_login(
    flow: Any,
    *,
    host: str = "127.0.0.1",
    port: int = CONTAINER_TOKEN_PORT,
    pairing_code: str = _PAIRING_CODE,
) -> Any:
    """Walk the two-step container login: address form, then pairing form.

    The setup flow asks for host/port first and for the pairing code only
    afterwards, because the container prints that code once the Google sign-in
    inside the noVNC session is done. Returns whatever the *address* step
    produced when it did not reach the pairing step, so a test that exercises
    the address half still sees its own result.
    """

    address = await _maybe_await(
        flow.async_step_container_login({"host": host, "port": port})
    )
    if not (
        isinstance(address, Mapping) and address.get("step_id") == "container_pairing"
    ):
        return address
    return await _maybe_await(
        flow.async_step_container_pairing({"pairing_code": pairing_code})
    )


# ---------------------------------------------------------------------------
# noVNC link rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        # Loopback / unspecified: Home Assistant reaches the container over the
        # host loopback, but the browser that opens noVNC usually runs on
        # another machine and never will. "0.0.0.0" is a bind wildcard, not a
        # browsable address at all.
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("0.0.0.0", "loopback"),
        # Concrete non-loopback literals: the one case where a link is honest.
        ("192.168.1.21", "linkable"),
        ("10.0.3.1", "linkable"),
        # Not an IP literal: container-only DNS (the compose service name) and
        # the `localhost` alias resolve inside HA, not necessarily in the
        # browser. The empty string is the "nothing entered yet" form state.
        ("localhost", "hostname"),
        ("googlefindmy-login", "hostname"),
        ("", "hostname"),
    ],
)
async def test_classify_novnc_host_buckets_by_browser_reachability(
    host: str, expected: str
) -> None:
    """The classifier sorts hosts by what a BROWSER can reach, not what HA can.

    ``localhost`` is deliberately *not* loopback here: the classification keys
    on "is this an IP literal that a remote browser can dial", and a name is
    resolved by whoever opens the link, so it can never be judged from here.
    """

    assert config_flow._classify_novnc_host(host) == expected


async def test_novnc_access_placeholder_links_only_linkable_hosts() -> None:
    """Only a non-loopback IP literal is rendered as a clickable markdown link.

    Offering a link that is known not to resolve in the operator's browser is
    worse than offering none: it turns a documentation problem into an apparent
    product failure. The fallback therefore stays inline code with a
    placeholder host, which cannot be clicked.
    """

    linkable = config_flow._novnc_access_placeholder("192.168.1.21")
    assert f"](http://192.168.1.21:{CONTAINER_NOVNC_PORT})" in linkable

    for host in ("127.0.0.1", "googlefindmy-login"):
        rendered = config_flow._novnc_access_placeholder(host)
        assert "](http" not in rendered, (
            f"{host!r} is not browser-reachable from here, so its noVNC hint must "
            f"not be a markdown link; got {rendered!r}"
        )


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
    """The container form defaults port to 7901 and hints noVNC on :7900."""

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

    # The noVNC placeholder targets the noVNC port, not the token port, and the
    # key is ALWAYS present so a translated description can reference it
    # unconditionally without risking a KeyError while rendering.
    placeholders = result.get("description_placeholders") or {}
    assert "novnc_access" in placeholders
    access = placeholders["novnc_access"]
    assert f":{CONTAINER_NOVNC_PORT}" in access
    assert CONTAINER_NOVNC_PORT == 7900
    # The default host is the loopback, which the operator's browser generally
    # cannot follow, so the hint must NOT be rendered as a clickable link.
    assert "](http" not in access


async def test_container_form_supplies_every_placeholder_the_text_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``{placeholder}`` in the shipped description must be supplied.

    The requirement is derived from ``strings.json`` rather than restated here,
    so it keeps holding when the translated text grows a new placeholder: an
    unsupplied one renders literally in the UI (``{docs_url}``), and hassfest
    forbids the alternative of writing the URL into the text itself. Reading the
    real file and driving the real step is what makes this a wiring check
    instead of a restatement of the code under test.
    """

    strings_path = Path(config_flow.__file__).resolve().parent / "strings.json"
    described = json.loads(strings_path.read_text(encoding="utf-8"))["config"]["step"][
        "container_login"
    ]["description"]
    required = set(re.findall(r"{([a-zA-Z0-9_]+)}", described))
    assert required, "the container_login description references no placeholder"

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    result = await _maybe_await(flow.async_step_container_login(None))
    assert isinstance(result, dict)
    supplied = set((result.get("description_placeholders") or {}).keys())

    assert required <= supplied, (
        "container_login renders placeholders it never supplies: "
        f"{sorted(required - supplied)}"
    )

    # Supplied but empty is the same defect one layer down: the sentence keeps
    # its shape and loses its content ("Full instructions:  (folder ...)").
    placeholders = result.get("description_placeholders") or {}
    empty = sorted(name for name in required if not str(placeholders[name]).strip())
    assert not empty, f"container_login supplies empty placeholders: {empty}"


async def test_address_and_pairing_code_are_asked_in_separate_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The address form must not ask for a code the container has not printed yet.

    The pairing code only exists after the Google sign-in inside the noVNC
    session, which the address collected in the first step is what makes
    reachable. So ``container_login`` carries host/port only and
    ``container_pairing`` carries the code only.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    address = await _maybe_await(flow.async_step_container_login(None))
    assert isinstance(address, dict)
    address_fields = {marker.schema for marker in address["data_schema"].schema}
    assert address_fields == {"host", "port"}
    assert "pairing_code" not in address_fields

    pairing = await _maybe_await(flow.async_step_container_pairing(None))
    assert isinstance(pairing, dict)
    assert pairing.get("step_id") == "container_pairing"
    pairing_fields = {marker.schema for marker in pairing["data_schema"].schema}
    assert pairing_fields == {"pairing_code"}

    # Nothing was fetched by merely rendering the two forms.
    assert recorder.fetch_calls == []


async def test_address_step_hands_host_and_port_to_the_pairing_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting the address form routes to the code form and keeps the address.

    The fetch happens in the second step, so host and port have to survive the
    step boundary in the flow state; otherwise the pairing step would silently
    query the default loopback address instead of the one the user entered.
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

    routed = await _maybe_await(
        flow.async_step_container_login({"host": "192.168.1.21", "port": 7999})
    )
    assert isinstance(routed, dict)
    assert routed.get("step_id") == "container_pairing"
    assert recorder.fetch_calls == []

    await _maybe_await(
        flow.async_step_container_pairing({"pairing_code": _PAIRING_CODE})
    )
    assert len(recorder.fetch_calls) == 1
    assert recorder.fetch_calls[0]["host"] == "192.168.1.21"
    assert recorder.fetch_calls[0]["port"] == 7999


async def test_unreachable_container_returns_to_the_address_step_keeping_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable container is an address problem, so the code is not lost.

    The remedy (fix host/port, start the container) lives in the *other* step,
    so the flow goes back there -- and the code the user already read off the
    launcher's terminal is prefilled when they arrive at the pairing form again.
    """

    recorder = _Recorder()
    _install_container_client(
        monkeypatch,
        recorder,
        fetch_raises=container_login.ContainerUnreachableError("boom"),
    )
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    await _maybe_await(
        flow.async_step_container_login({"host": "10.0.3.1", "port": 7901})
    )
    back = await _maybe_await(
        flow.async_step_container_pairing({"pairing_code": _PAIRING_CODE})
    )
    assert isinstance(back, dict)
    assert back.get("step_id") == "container_login"
    assert back.get("errors") == {"base": "container_unreachable"}
    # The address the user entered is still the form's default...
    host_default = next(
        marker.default()
        for marker in back["data_schema"].schema
        if getattr(marker, "schema", None) == "host"
    )
    assert host_default == "10.0.3.1"

    # ...and the pairing code survives the detour instead of having to be
    # re-read from the launcher's terminal.
    again = await _maybe_await(flow.async_step_container_pairing(None))
    code_default = next(
        marker.default()
        for marker in again["data_schema"].schema
        if getattr(marker, "schema", None) == "pairing_code"
    )
    assert code_default == _PAIRING_CODE


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

    result = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
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


async def test_pending_ack_is_handed_over_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deferred ack is handed to the durability gate exactly once (F4/P2).

    Drives ``container_login`` to stage the pending ack, then invokes the
    hand-over helper that ``device_selection`` calls right after
    ``async_create_entry``. The ack must land on exactly one staged job and the
    pending slot must be cleared, so a second hand-over is a no-op (no
    double-ack). Nothing may be sent: at this point Home Assistant has not
    stored anything yet.
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

    await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
    # Not acked while the entry is not yet created.
    assert recorder.ack_calls == []
    assert flow._container_pending_ack is not None

    # Simulate device_selection reaching CREATE_ENTRY and handing the ack over.
    flow._async_stage_container_ack()
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert len(staged[0].jobs) == 1
    assert staged[0].jobs[0].ack is not None
    assert staged[0].jobs[0].ack.delete_token == _DELETE_TOKEN
    # A create-path ticket names no entry: the gate only has to see the entry
    # appear in storage at all.
    assert staged[0].entry_id is None
    assert staged[0].min_modified_at is None
    # Still nothing sent to the container.
    assert recorder.ack_calls == []
    # Cleared: a second hand-over is a no-op (no double-ack, no second job).
    assert flow._container_pending_ack is None
    flow._async_stage_container_ack()
    assert len(_staged_cleanup(hass)[0].jobs) == 1
    assert recorder.ack_calls == []


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

    await _drive_container_login(flow, pairing_code=_PAIRING_CODE)

    # The credential is staged but the container was NOT told to delete it.
    assert flow._container_pending_ack is not None
    assert recorder.ack_calls == []


def _staged_cleanup(hass: Any) -> list[Any]:
    """Return the in-memory staging area the flow writes its cleanup tickets to.

    A FIFO list of per-flow tickets, not a mapping keyed by account: two
    overlapping create flows for the same account must stay separable.
    """

    bucket = hass.data.get(config_flow.DOMAIN) or {}
    staged = bucket.get(config_flow.PENDING_CONTAINER_CLEANUP_KEY) or []
    assert isinstance(staged, list)
    return staged


async def _run_staged_cleanup(
    hass: Any, *, unique_id: str | None, entry_id: str | None = None
) -> None:
    """Claim one staged ticket and execute it, as the durability gate does.

    Production never runs the jobs inline: ``async_setup_entry`` only claims the
    ticket and arms a background task that waits for proof that Home Assistant's
    storage holds the state that authorises the cleanup (see
    ``config_flow.async_schedule_pending_container_cleanup``). These tests cover
    the job semantics *after* that proof, so they drive the two halves directly
    instead of standing up HA's storage against a hand-built ``hass`` double.

    ``entry_id`` is required for the tickets of the *update* paths: those name
    their entry, and a claim that does not name the same entry must not get
    them.
    """

    jobs = config_flow._async_claim_container_cleanup(
        hass, unique_id=unique_id, entry_id=entry_id
    )
    await config_flow._async_execute_container_cleanup(hass, jobs)


async def test_create_entry_stages_ack_instead_of_sending_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: reaching CREATE_ENTRY stages the ack, it does NOT send it.

    ``ConfigFlow.async_create_entry`` only builds a FlowResult -- Home Assistant
    creates and stores the entry afterwards in
    ``ConfigEntriesFlowManager.async_finish_flow`` (``await
    self.config_entries.async_add(entry)``). Acking here would tell the login
    container to drop the only remaining copy of the credentials while the
    entry may still fail to materialise, so the ack is handed to
    ``async_setup_entry`` through ``hass.data`` instead.

    Drives the *real* ``device_selection`` step to CREATE_ENTRY, so the staging
    is observed at the actual call site rather than through a stub.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

    # container_login -> real device_selection form (no entry yet).
    form = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
    assert isinstance(form, dict)
    assert form.get("step_id") == "device_selection"
    assert recorder.ack_calls == []
    assert _staged_cleanup(hass) == []

    # Submitting the form reaches async_create_entry.
    created = await _maybe_await(flow.async_step_device_selection({}))
    assert isinstance(created, dict)
    assert created.get("type") == "create_entry"

    # Still no ack: the container keeps its copy until the entry exists.
    assert recorder.ack_calls == []
    # The flow-local slot is cleared (no double staging on a retry) and the job
    # is parked under the flow's unique id, which Home Assistant copies onto the
    # new entry verbatim.
    assert flow._container_pending_ack is None
    staged = _staged_cleanup(hass)
    assert [ticket.unique_id for ticket in staged] == [flow.unique_id]
    # One ticket for this one flow, addressed by the flow's own id: that is what
    # keeps a second, concurrent flow for the same account separable.
    assert staged[0].flow_id == flow._async_cleanup_ticket_id()
    jobs = staged[0].jobs
    assert len(jobs) == 1
    assert isinstance(jobs[0], config_flow.PendingContainerCleanup)

    # The staged job addresses the ack but carries no credentials: only the
    # container coordinates plus the one-shot delete authorisation.
    job = jobs[0]
    assert job.imported_stable_key is None
    assert job.imported_digest is None
    ack = job.ack
    assert ack is not None
    assert (ack.host, ack.port, ack.pairing_code, ack.delete_token) == (
        "127.0.0.1",
        CONTAINER_TOKEN_PORT,
        _PAIRING_CODE,
        _DELETE_TOKEN,
    )
    # The only extra passenger is the fetch timestamp, which merely grades the
    # log level of a failed ack (TTL race vs. real error); no credentials.
    assert isinstance(ack.fetched_monotonic, float)
    assert not hasattr(ack, "parsed")
    assert not hasattr(ack, "token")


async def test_staged_ack_is_sent_by_the_cleanup_runner_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner ``async_setup_entry`` calls sends the staged ack once.

    ``async_setup_entry`` re-runs on every reload, so the runner must consume
    (``pop``) the staged jobs. A second run must stay silent.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

    await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
    await _maybe_await(flow.async_step_device_selection({}))
    assert recorder.ack_calls == []

    unique_id = flow.unique_id
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.ack_calls) == 1
    assert recorder.ack_calls[0]["delete_token"] == _DELETE_TOKEN
    assert _staged_cleanup(hass) == []

    # Reload: nothing left to do, so no second ack.
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.ack_calls) == 1


async def test_cleanup_runner_ignores_jobs_of_other_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job staged for account A must not be executed by account B's setup.

    Multi-account setups run one ``async_setup_entry`` per entry; resolving the
    staged jobs by unique id keeps a second account's entry from acking a
    container login that is still waiting for its own entry.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-account-a",
        unique_id="account-a@example.com",
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
    )

    await _run_staged_cleanup(hass, unique_id="account-b@example.com")
    assert recorder.ack_calls == []
    assert [ticket.unique_id for ticket in _staged_cleanup(hass)] == [
        "account-a@example.com"
    ]

    await _run_staged_cleanup(hass, unique_id="account-a@example.com")
    assert len(recorder.ack_calls) == 1


# ---------------------------------------------------------------------------
# Ticket correlation: an aborted flow must not leave a ticket behind
# ---------------------------------------------------------------------------
#
# Home Assistant aborts every competing in-progress flow with the same unique id
# when one flow finishes (``ConfigEntriesFlowManager.async_finish_flow``:
# ``self.async_abort(progress_flow_id)``), and every flow ending -- abort and
# success alike -- runs through ``FlowManager._async_remove_flow_progress``,
# which calls the overridable ``FlowHandler.async_remove`` hook. Selecting a
# ticket by "same account, oldest first" therefore cannot separate two
# overlapping flows: the loser's ticket outlives it and the winner's entry would
# claim it. The flow drops its own ticket instead.


def _stage_ack_ticket(hass: Any, *, flow_id: str, unique_id: str | None) -> None:
    """Stage one ack job on ``flow_id``'s ticket, as a create flow would."""

    config_flow._async_stage_container_cleanup(
        hass,
        flow_id=flow_id,
        unique_id=unique_id,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
    )


async def test_aborted_flow_takes_its_ticket_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loser of two same-account flows must not leave its ticket behind.

    The exact interleaving Home Assistant produces: two flows for one account
    stage a ticket each, the second one is aborted (Core aborts the competing
    in-progress flow when the first finishes) and only the first ever produces
    an entry. Without the removal hook that entry would find TWO claimable
    tickets for its account and, on this or a later reload, ack credentials that
    belong to a flow which never created anything.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    winner = config_flow.ConfigFlow()
    winner.hass = hass  # type: ignore[assignment]
    winner.context = {}
    winner.flow_id = "flow-winner"  # type: ignore[attr-defined]
    loser = config_flow.ConfigFlow()
    loser.hass = hass  # type: ignore[assignment]
    loser.context = {}
    loser.flow_id = "flow-loser"  # type: ignore[attr-defined]

    _stage_ack_ticket(hass, flow_id=winner._async_cleanup_ticket_id(), unique_id=_EMAIL)
    _stage_ack_ticket(hass, flow_id=loser._async_cleanup_ticket_id(), unique_id=_EMAIL)
    assert len(_staged_cleanup(hass)) == 2

    # Home Assistant removes the aborted flow; the hook drops its ticket.
    loser.async_remove()
    assert [ticket.flow_id for ticket in _staged_cleanup(hass)] == ["flow-winner"]

    # The winner's entry claims its own ticket -- and there is nothing else to
    # claim afterwards, on this reload or any later one.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert len(recorder.ack_calls) == 1
    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert len(recorder.ack_calls) == 1


async def test_removing_a_successful_flow_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removal after a successful create must not touch anyone else's ticket.

    Home Assistant calls ``async_remove`` for a successful flow too, but only
    after ``async_finish_flow`` added the entry, i.e. after
    ``async_setup_entry`` claimed this flow's ticket. The hook must therefore
    find nothing of its own -- and must leave a concurrent flow's ticket alone
    rather than clearing the staging area.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    done = config_flow.ConfigFlow()
    done.hass = hass  # type: ignore[assignment]
    done.context = {}
    done.flow_id = "flow-done"  # type: ignore[attr-defined]

    _stage_ack_ticket(hass, flow_id="flow-done", unique_id=_EMAIL)
    _stage_ack_ticket(hass, flow_id="flow-other", unique_id="other@example.com")

    # The entry created by this flow already claimed its ticket.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert len(recorder.ack_calls) == 1

    done.async_remove()
    assert [ticket.flow_id for ticket in _staged_cleanup(hass)] == ["flow-other"]


async def test_removing_a_flow_keeps_its_update_path_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An update-path ticket survives the removal of the flow that staged it.

    Reauth, reconfigure and the options credential refresh all *end* by aborting
    the flow on purpose, right after they updated the entry and scheduled its
    reload. Their ticket names that entry and is waiting for the reload's
    ``async_setup_entry``; discarding it on removal would disable the cleanup on
    every update path. The distinction is exactly ``entry_id is None``.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow.flow_id = "flow-update"  # type: ignore[attr-defined]

    entry = make_config_entry(entry_id="entry-update", unique_id=_EMAIL)
    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id=flow._async_cleanup_ticket_id(),
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
        entry=entry,
    )

    flow.async_remove()
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == "entry-update"

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-update")
    assert len(recorder.ack_calls) == 1


async def test_removing_a_flow_keeps_the_ticket_of_a_retrying_entry_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A created entry whose FIRST setup retries must keep its ticket.

    ``ConfigEntries.async_add`` awaits ``async_setup``, so the first
    ``async_setup_entry`` runs *inside* ``async_finish_flow``, i.e. before Home
    Assistant removes the flow. When that attempt raises
    ``ConfigEntryNotReady`` it never reaches the ticket claim at the end of
    setup: Home Assistant catches the exception, schedules a retry and removes
    the flow all the same. Flow removal is therefore ambiguous -- "no entry,
    ever" and "entry created, setup retrying" arrive through the very same
    hook -- and dropping the ticket here would leave the imported
    ``secrets.json`` on disk forever and the login container un-acked, because
    the later successful retry finds nothing to claim.

    Drives the *real* ``device_selection`` step to CREATE_ENTRY so the promise
    marker is observed at its actual call site, not through a hand-set flag.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]
    flow._abort_if_unique_id_configured = lambda **_: None  # type: ignore[attr-defined]

    await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
    created = await _maybe_await(flow.async_step_device_selection({}))
    assert created.get("type") == "create_entry"

    unique_id = flow.unique_id
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    # No entry id yet -- the flow cannot know it -- but the promise is recorded.
    assert staged[0].entry_id is None
    assert staged[0].entry_promised is True

    # First async_setup_entry raised ConfigEntryNotReady: nothing was claimed,
    # and Home Assistant removes the flow anyway.
    flow.async_remove()

    kept = _staged_cleanup(hass)
    assert len(kept) == 1, "a retrying entry setup must keep its cleanup ticket"
    assert kept[0].flow_id == flow._async_cleanup_ticket_id()
    assert recorder.ack_calls == []

    # The retry succeeds and claims the surviving ticket exactly once.
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.ack_calls) == 1
    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=unique_id)
    assert len(recorder.ack_calls) == 1


async def test_removing_an_aborted_flow_still_drops_its_unpromised_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The promise marker must not blunt the abort path it shares a hook with.

    Counterpart to the test above and the reason the marker is set at
    CREATE_ENTRY rather than when the ticket is staged: a flow that staged its
    jobs but never reached CREATE_ENTRY has no entry and never will, so its
    ticket must still be dropped -- otherwise a competing same-account entry
    inherits it and acks credentials that belong to the aborted flow.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    aborted = config_flow.ConfigFlow()
    aborted.hass = hass  # type: ignore[assignment]
    aborted.context = {}
    aborted.flow_id = "flow-aborted"  # type: ignore[attr-defined]

    _stage_ack_ticket(hass, flow_id="flow-aborted", unique_id=_EMAIL)
    assert _staged_cleanup(hass)[0].entry_promised is False

    aborted.async_remove()

    assert _staged_cleanup(hass) == []
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.ack_calls == []


async def test_marking_an_entry_promise_without_a_staging_area_is_a_noop() -> None:
    """The promise marker must tolerate an empty staging area.

    A flow can reach CREATE_ENTRY without ever staging a cleanup job (manual
    credentials, no login container), so the marker has nothing to mark. It must
    then stay silent instead of creating a bucket or raising.
    """

    hass = _build_hass([])

    assert config_flow._async_mark_cleanup_ticket_entry_promised(hass, "flow-x") == 0
    assert config_flow._async_mark_cleanup_ticket_entry_promised(None, "flow-x") == 0
    assert _staged_cleanup(hass) == []


async def test_removing_an_options_flow_drops_its_uncorrelated_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OptionsFlowHandler`` needs the same removal guard as ``ConfigFlow``.

    Today the options credential refresh only ever stages a ticket that names
    its entry, so this override is a no-op there. It exists for the next options
    path that stages an uncorrelated ticket, and without a test the guard could
    be deleted or moved without anything turning red.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    # Bypass __init__: the handler's constructor wants a config entry, and this
    # test is about the removal hook, not about flow construction.
    flow = object.__new__(config_flow.OptionsFlowHandler)
    flow.hass = hass  # type: ignore[attr-defined]
    flow.context = {}  # type: ignore[attr-defined]
    flow.flow_id = "options-flow"  # type: ignore[attr-defined]
    flow._container_cleanup_ticket_id = None  # type: ignore[attr-defined]

    staged_ok = config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id=flow._async_cleanup_ticket_id(),
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
        entry=None,
    )
    assert staged_ok is True
    assert len(_staged_cleanup(hass)) == 1

    # Without a hass there is nowhere to stage, and the helper must say so
    # instead of reporting a job it silently dropped.
    assert (
        config_flow._async_stage_container_cleanup_for(
            None,
            flow_id="flow-without-hass",
            unique_id=_EMAIL,
            job=config_flow.PendingContainerCleanup(
                ack=config_flow._ContainerAckTarget(
                    host="127.0.0.1",
                    port=CONTAINER_TOKEN_PORT,
                    pairing_code=_PAIRING_CODE,
                    delete_token=_DELETE_TOKEN,
                )
            ),
            entry=None,
        )
        is False
    )
    assert len(_staged_cleanup(hass)) == 1

    flow.async_remove()

    assert _staged_cleanup(hass) == []
    assert recorder.ack_calls == []


async def test_options_flow_defines_the_removal_hook_ahead_of_the_mixin() -> None:
    """Pin *why* the hook has to sit on the concrete class.

    ``data_entry_flow.FlowHandler`` precedes ``_ContainerLoginMixin`` in this
    MRO, so a mixin-level override would never be reached: Python would keep
    finding the base class's no-op first. Moving the method to the mixin would
    silently disable it, and only this assertion notices.
    """

    assert "async_remove" in config_flow.OptionsFlowHandler.__dict__

    fallback = next(
        (
            klass
            for klass in config_flow.OptionsFlowHandler.__mro__[1:]
            if "async_remove" in klass.__dict__
        ),
        None,
    )
    assert fallback is not None
    assert fallback is not config_flow._ContainerLoginMixin


async def test_an_update_ticket_is_never_claimed_by_another_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ticket that names its entry is invisible to every other entry.

    Two entries of the same account can coexist during a replace, and an entry
    without a unique id claims the account-less fallback. Neither may reach a
    ticket that was addressed to a specific entry.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    entry = make_config_entry(entry_id="entry-a", unique_id=_EMAIL)
    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
        entry=entry,
    )

    # Same account, different entry: no.
    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-b")
    # Account-less fallback: no.
    await _run_staged_cleanup(hass, unique_id=None)
    # Right account, no entry id at all (the create-path claim): still no.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.ack_calls == []
    assert len(_staged_cleanup(hass)) == 1

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-a")
    assert len(recorder.ack_calls) == 1


async def test_update_cleanup_is_dropped_without_a_durability_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``modified_at`` means no proof, so the job is never staged at all.

    The gate for an update path can only be honest with a watermark: the entry
    id has been in storage since long before the update, so "the entry exists"
    would authorise the irreversible cleanup on no evidence. An entry that
    cannot supply one therefore loses its cleanup -- credentials survive, the
    container falls back to its TTL delete.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    entry = make_config_entry(entry_id="entry-no-watermark", unique_id=_EMAIL)
    del entry.modified_at

    staged = config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(imported_digest="deadbeef"),
        entry=entry,
    )
    assert staged is False
    assert _staged_cleanup(hass) == []

    await _run_staged_cleanup(hass, unique_id=_EMAIL, entry_id="entry-no-watermark")
    assert recorder.ack_calls == []


async def test_scheduler_hands_the_watermark_to_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async_setup_entry`` must pass the ticket's watermark to the probe.

    The claim and the proof live in two different functions; if the watermark
    were dropped between them the update paths would silently fall back to the
    vacuous "entry id exists" proof, and every test above would still pass.
    """

    hass = _build_hass([])
    entry = make_config_entry(entry_id="entry-gate", unique_id=_EMAIL)
    watermark = entry.modified_at

    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id="flow-update",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(imported_digest="deadbeef"),
        entry=entry,
    )

    seen: list[tuple[str, Any]] = []

    async def _fake_probe(
        _hass: Any, entry_id: str, *, min_modified_at: Any = None
    ) -> bool:
        seen.append((entry_id, min_modified_at))
        return True

    executed: list[list[Any]] = []

    async def _fake_execute(_hass: Any, jobs: list[Any]) -> None:
        executed.append(jobs)

    monkeypatch.setattr(config_flow, "_async_config_entry_is_persisted", _fake_probe)
    monkeypatch.setattr(config_flow, "_async_execute_container_cleanup", _fake_execute)

    def _create_background_task(_hass: Any, coro: Any, **_kw: Any) -> Any:
        return asyncio.ensure_future(coro)

    entry.async_create_background_task = _create_background_task

    task = config_flow.async_schedule_pending_container_cleanup(hass, entry)
    assert task is not None
    await task
    # The entry id AND the watermark reached the probe, and only then did the
    # irreversible half run.
    assert seen == [("entry-gate", watermark)]
    assert len(executed) == 1
    assert executed[0][0].imported_digest == "deadbeef"


async def test_cleanup_runner_is_a_noop_without_staged_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry that never went through a container login acks nothing."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    # No hass.data[DOMAIN] bucket at all.
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    # Bucket present but no staging area.
    hass.data[config_flow.DOMAIN] = {}
    await _run_staged_cleanup(hass, unique_id=_EMAIL)
    assert recorder.ack_calls == []


async def test_failing_ack_is_logged_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A container that refuses the ack degrades to its TTL delete, silently.

    The ack is the *second* phase of the delete: the credentials are already in
    the config entry, so a failed ack costs nothing but an orphaned file that
    the container drops on its own TTL. Raising here would break an otherwise
    completed flow, and the log must not leak the nonce or the delete token.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    async def _failing_ack(*_args: Any, **_kwargs: Any) -> None:
        raise config_flow.ContainerUnreachableError("container gone")

    monkeypatch.setattr(config_flow, "ack_consumed", _failing_ack)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-failing-ack",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
    )

    with caplog.at_level(logging.WARNING):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    assert "ContainerUnreachableError" in caplog.text
    assert _PAIRING_CODE not in caplog.text
    assert _DELETE_TOKEN not in caplog.text
    # Consumed regardless: the container's TTL fallback owns it from here.
    assert _staged_cleanup(hass) == []


@pytest.mark.parametrize(
    ("age_offset", "expect_warning"),
    [
        # Older than the container's TTL: the endpoint is gone because the TTL
        # deleted the secret and shut the server down. Expected ending, not an
        # error.
        (CONTAINER_TOKEN_TTL + 1, False),
        # Still inside the TTL: the endpoint should be up, so an unreachable
        # container is a genuine problem and stays a warning.
        (1, True),
    ],
)
async def test_ack_after_ttl_expiry_logs_at_debug_not_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    age_offset: float,
    expect_warning: bool,
) -> None:
    """A lost race against the container TTL is normal and must not warn.

    The ack fires at the very end of ``async_setup_entry`` (after the coordinator
    refresh, the FCM registration and the platform forward), while the container
    deletes its copy ``CONTAINER_TOKEN_TTL`` seconds after *its* start no matter
    what. On a slow instance the TTL therefore wins routinely. It deletes the
    same file the ack would have asked for, so the outcome is identical and a
    warning would be misleading noise.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    async def _failing_ack(*_args: Any, **_kwargs: Any) -> None:
        raise config_flow.ContainerUnreachableError("container gone")

    monkeypatch.setattr(config_flow, "ack_consumed", _failing_ack)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-ttl-debug",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
                fetched_monotonic=time.monotonic() - age_offset,
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=config_flow._LOGGER.name):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert bool(warnings) is expect_warning
    # Either way the failure is reported somewhere and stays free of secrets.
    assert "ContainerUnreachableError" in caplog.text
    assert _PAIRING_CODE not in caplog.text
    assert _DELETE_TOKEN not in caplog.text


async def test_ack_lockout_warning_does_not_promise_a_ttl_delete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A *proven* lockout means the container KEEPS the file, so say that.

    A locked-out server deliberately preserves ``secrets.json`` and then exits,
    so nothing deletes it afterwards. The generic "expected to fall back to its
    TTL delete" wording would therefore tell the operator the credential cleans
    itself up while it actually stays on disk.

    The error is built the way ``_require_ack_deleted`` builds it -- with
    ``secret_retained=True`` -- because that flag, not the error class, is what
    the branch keys on (see
    ``test_only_a_proven_lockout_claims_a_retained_secret`` for the sibling
    ``ContainerAuthError`` that must NOT reach this message).
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    async def _locked_out_ack(*_args: Any, **_kwargs: Any) -> None:
        raise config_flow.ContainerAuthError(
            "ack rejected: endpoint locked", secret_retained=True
        )

    monkeypatch.setattr(config_flow, "ack_consumed", _locked_out_ack)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-lockout",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
                # Well inside the TTL: the "the TTL already deleted it" excuse
                # must not be reachable here.
                fetched_monotonic=time.monotonic(),
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=config_flow._LOGGER.name):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert warnings, "a rejected ack leaves a credential behind and must warn"
    text = "\n".join(record.getMessage() for record in warnings)
    assert "ContainerAuthError" in text
    assert "lockout" in text
    # The operator has to act, so the message has to say so.
    assert "manually" in text
    # The whole point: no false promise of an automatic cleanup.
    assert "TTL delete" not in text
    assert _PAIRING_CODE not in caplog.text
    assert _DELETE_TOKEN not in caplog.text


async def test_staging_without_hass_is_a_noop() -> None:
    """Staging before the flow is bound to hass must not raise.

    Fail-safe direction: with nowhere to park the job the cleanup simply never
    happens, which leaves the credential file in place for the next import.
    """

    config_flow._async_stage_container_cleanup(
        None,
        flow_id="flow-no-hass",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(imported_digest="deadbeef"),
    )


async def test_stage_container_ack_without_pending_result_is_a_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow that never used the container login stages nothing on create."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    assert flow._container_pending_ack is None
    flow._async_stage_container_ack()
    assert _staged_cleanup(hass) == []
    assert recorder.ack_calls == []


async def test_reconfigure_persist_stages_the_ack_behind_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconfigure path stages its ack; it must not send it (Codex P2, fund A).

    ``hass.config_entries.async_update_entry`` does NOT write through: it
    mutates the in-memory entry and schedules Home Assistant's debounced store
    save (``_async_save_and_notify`` -> ``_async_schedule_save`` ->
    ``Store.async_delay_save``). Acking inside that window would tell the login
    container to drop the only other copy of the credentials before Home
    Assistant committed them.

    Drives the *production* branch, not a helper: a container login followed by
    ``async_step_device_selection`` with ``is_reconfigure`` in the flow context.
    Also pins the correlation, because that is what makes the deferred proof
    non-vacuous: the ticket names this entry and carries its ``modified_at``, so
    the gate waits for a stored record at least that recent instead of for the
    entry id, which was in storage all along.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reconfigure-inline",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
        unique_id=_EMAIL,
        # The reconfigure branch walks the entry's subentries; an empty mapping
        # is the "nothing to sync" case and keeps the test on its subject.
        subentries={},
    )
    hass = _build_hass([entry])

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    # Home Assistant sets this context when the user reconfigures an existing
    # entry; it is what selects the inline-persist branch further down.
    flow.context = {"is_reconfigure": True, "entry_id": entry.entry_id}
    # A probe would need a live API; the device list is not what is under test.
    flow._available_devices = [("Device", "device-id")]  # type: ignore[attr-defined]

    await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
    # The fetch stages the ack on the flow; nothing has been sent yet.
    assert flow._container_pending_ack is not None
    assert recorder.ack_calls == []

    # Finish through the reconfigure branch of the real step.
    result = await _maybe_await(flow.async_step_device_selection({}))

    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert result.get("reason") == "reconfigure_successful"
    # The entry update really happened in this branch.
    assert any(update["entry"] is entry for update in hass.config_entries.updated)
    # NOT acked: the container still holds its copy while the save is pending.
    assert recorder.ack_calls == []
    assert flow._container_pending_ack is None
    # Staged instead, addressed to this entry and gated on its watermark.
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == entry.entry_id
    assert staged[0].min_modified_at == entry.modified_at
    assert len(staged[0].jobs) == 1
    assert staged[0].jobs[0].ack is not None
    assert staged[0].jobs[0].ack.delete_token == _DELETE_TOKEN


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

    result = await _drive_container_login(flow, pairing_code="   ")
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

    result = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
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
    result = await _drive_container_login(flow, pairing_code=short_code)
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

    result = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
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
    result = await _drive_container_login(flow, pairing_code=wrong_but_well_formed)
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
        await _drive_container_login(flow, pairing_code=_PAIRING_CODE)

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

    result = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)
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
    """The reauth container branch fetches, updates, and STAGES the ack.

    ``async_update_reload_and_abort`` wraps ``async_update_entry``, whose save is
    debounced, so the ack has to wait behind the durability gate that the reload
    it schedules will arm (Codex P2, fund A).
    """

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
    # Staged, not sent, and addressed to the entry it belongs to.
    assert recorder.ack_calls == []
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == entry.entry_id
    assert staged[0].min_modified_at == entry.modified_at
    assert staged[0].jobs[0].ack is not None


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
# Credential-method exclusivity (reauth)
# ---------------------------------------------------------------------------
#
# The container GET is ONE-SHOT: the token server hands out the bundle once and
# then locks the pairing code. A submission that carries a pasted secrets bundle
# *and* a pairing code therefore must not be resolved by precedence -- whichever
# method lost would be silently discarded, and if the container won, the code is
# burned for a bundle the user did not even want. Both forms reject the mixed
# submission with ``choose_one`` before any request leaves the process.


def _secrets_json() -> str:
    """Serialize the valid bundle the way a user would paste it into the form."""

    return json.dumps(_valid_bundle())


def _make_reauth_flow(hass: Any, entry: Any, captured: dict[str, Any]) -> Any:
    """Build a reauth flow whose persist point is observable.

    Same stubbing as ``test_reauth_container_branch_happy_path``:
    ``async_update_reload_and_abort`` is the reauth persist point and is
    replaced by a recorder, and the cache clear is a no-op because no runtime
    data exists in these doubles.
    """

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}

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
    return flow


async def test_reauth_rejects_two_credential_methods_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secrets JSON *and* a pairing code -> ``choose_one``, and no container call.

    The container client is the same mock the rest of this module uses, so the
    "no network" claim is proven by the recorder staying empty -- not by the
    absence of an exception.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reauth-exclusive",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    captured: dict[str, Any] = {}
    flow = _make_reauth_flow(hass, entry, captured)

    result = await _maybe_await(
        flow.async_step_reauth_confirm(
            {
                "secrets_json": _secrets_json(),
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "choose_one"}
    # The whole point: the one-shot code was NOT spent, and nothing was
    # persisted from the pasted bundle either.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []
    assert "data" not in captured


async def test_reauth_with_only_a_pairing_code_takes_the_container_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control (b) for the gate above: one method still routes to the container."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reauth-container-only",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    captured: dict[str, Any] = {}
    flow = _make_reauth_flow(hass, entry, captured)

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
    assert len(recorder.fetch_calls) == 1
    assert captured["data"][DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX


async def test_reauth_with_only_secrets_json_takes_the_secrets_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control (c): one method still routes to the pasted-bundle path, no fetch."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reauth-secrets-only",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    captured: dict[str, Any] = {}
    flow = _make_reauth_flow(hass, entry, captured)

    result = await _maybe_await(
        flow.async_step_reauth_confirm({"secrets_json": _secrets_json()})
    )
    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert captured["data"][DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    # The secrets path must not talk to a login container at all.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []


# ---------------------------------------------------------------------------
# Options container branch
# ---------------------------------------------------------------------------


async def test_options_container_branch_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options container branch updates the entry and STAGES the ack.

    ``async_update_entry`` only schedules the debounced store save, so the ack
    goes to the durability gate that the reload scheduled here will arm
    (Codex P2, fund A).
    """

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
    # Wrote the validated bundle onto the entry and staged (not sent) the ack.
    assert entry.data[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    assert entry.data[CONF_OAUTH_TOKEN] == _TOKEN
    assert recorder.ack_calls == []
    staged = _staged_cleanup(hass)
    assert len(staged) == 1
    assert staged[0].entry_id == entry.entry_id
    assert staged[0].min_modified_at == entry.modified_at
    assert staged[0].jobs[0].ack is not None


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
    """F-N4: OptionsFlowHandler must inherit the shared container helpers.

    The options credential-refresh path calls ``_async_container_fetch`` and
    ``_async_stage_container_ack_result`` (both on ``_ContainerLoginMixin``), and
    the latter needs ``_async_cleanup_ticket_id`` from the same mixin. If any of
    them were reachable only on ``ConfigFlow`` the options persist would raise
    ``AttributeError`` before any request. This asserts they are bound callables
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
    assert callable(getattr(flow, "_async_stage_container_ack_result", None))
    assert callable(getattr(flow, "_async_cleanup_ticket_id", None))

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
    # fetch validated the bundle and the ack was staged after the update
    # (order: fetch -> update_entry -> stage), so exactly one of each ran.
    assert len(recorder.fetch_calls) == 1
    assert recorder.ack_calls == []
    assert len(_staged_cleanup(hass)) == 1


async def test_options_container_persist_stages_before_it_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options branch stages the ack, and stages it BEFORE the reload.

    Order matters as much as the staging itself: the reload this branch
    schedules is what runs ``async_setup_entry`` and therefore what claims the
    ticket. A ticket staged after the reload was scheduled would sit unclaimed
    until some later reload, which silently turns the cleanup into a leak.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-options-inline",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    def _abort(*, reason: str, **_: Any) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]
    flow.async_abort = _abort  # type: ignore[assignment]

    # Observe the staging area at the exact moment the reload is scheduled.
    # ``async_setup_entry`` claims the ticket during that reload, so a ticket
    # that is not staged yet at this point would never be claimed by it.
    observed: list[int] = []
    inner_create_task = hass.async_create_task

    def _recording_create_task(coro: Any, *args: Any, **kwargs: Any) -> Any:
        observed.append(len(_staged_cleanup(hass)))
        return inner_create_task(coro, *args, **kwargs)

    hass.async_create_task = _recording_create_task  # type: ignore[method-assign]

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
    assert result.get("reason") == "reconfigure_successful"
    assert recorder.ack_calls == []
    # Staged for async_setup_entry, and staged before the reload was scheduled.
    assert len(_staged_cleanup(hass)) == 1
    assert observed == [1]


# ---------------------------------------------------------------------------
# Credential-method exclusivity (options)
# ---------------------------------------------------------------------------
#
# Same rule as the reauth form above, enforced in ``async_step_credentials``.
# There it is expressed as ``supplied != 1``, which folds the pre-existing
# "nothing entered" case and the new "more than one method" case into the same
# ``choose_one`` verdict. These tests drive the REAL step (not the persist
# helper), because the gate lives in the step.


def _make_options_flow(hass: Any, entry: Any) -> Any:
    """Build an options flow with the persist side effects stubbed out."""

    flow = config_flow.OptionsFlowHandler()
    flow.hass = hass  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]

    async def _clear_cached_aas_token(_entry: Any) -> None:
        return None

    async def _refresh_title(_entry: Any, _opt: Any) -> None:
        return None

    def _abort(*, reason: str, **_: Any) -> dict[str, Any]:
        return {"type": "abort", "reason": reason}

    flow._async_clear_cached_aas_token = _clear_cached_aas_token  # type: ignore[attr-defined]
    flow._async_refresh_subentry_entry_title = _refresh_title  # type: ignore[attr-defined]
    flow.async_abort = _abort  # type: ignore[assignment]
    return flow


def _options_entry(entry_id: str) -> Any:
    """A config entry double for the options credentials step.

    ``subentries={}`` is the "no feature groups configured" case, for which the
    step synthesises the single ``core_tracking`` choice; the subentry selector
    is not what these tests are about.
    """

    return make_config_entry(
        entry_id=entry_id,
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
        unique_id=_EMAIL,
        subentries={},
    )


async def test_options_rejects_two_credential_methods_before_any_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secrets JSON *and* a pairing code -> ``choose_one``, and no container call."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = _options_entry("entry-options-exclusive")
    hass = _build_hass([entry])
    flow = _make_options_flow(hass, entry)

    result = await _maybe_await(
        flow.async_step_credentials(
            {
                "new_secrets_json": _secrets_json(),
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "choose_one"}
    # Neither method ran: the pairing code is still usable for a clean retry and
    # the entry was left untouched.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []
    assert DATA_SECRET_BUNDLE not in entry.data


async def test_options_with_only_a_pairing_code_takes_the_container_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control (b): one method still routes to the container persist."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = _options_entry("entry-options-container-only")
    hass = _build_hass([entry])
    flow = _make_options_flow(hass, entry)

    result = await _maybe_await(
        flow.async_step_credentials(
            {
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert result.get("reason") == "reconfigure_successful"
    assert len(recorder.fetch_calls) == 1
    assert entry.data[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX


async def test_options_with_only_secrets_json_takes_the_secrets_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control (c): one method still routes to the pasted-bundle path, no fetch."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = _options_entry("entry-options-secrets-only")
    hass = _build_hass([entry])
    flow = _make_options_flow(hass, entry)

    result = await _maybe_await(
        flow.async_step_credentials({"new_secrets_json": _secrets_json()})
    )
    assert isinstance(result, dict)
    assert result.get("type") == "abort"
    assert result.get("reason") == "reconfigure_successful"
    assert entry.data[DATA_SECRET_BUNDLE]["shared_key"] == _SHARED_HEX
    # The secrets path must not talk to a login container at all.
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []


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
    """Stand-in for ``response.content`` with chunk-wise ``StreamReader`` reads.

    ``read(n)`` hands back *up to* n bytes of the next queued chunk and stops at
    the chunk boundary, so EOF is only ever signalled by an empty result. The
    real ``StreamReader`` may coalesce *already buffered* chunks up to n bytes
    (``streams.py::_read_nowait``); this double deliberately models the
    pessimistic case that the network actually produces -- a short read at every
    TCP boundary -- because that is what a single ``read()`` call trips over.

    ``chunk_size`` splits the body so a test can force that multi-chunk arrival.
    ``read_calls`` and ``bytes_served`` are the instrumentation the regression
    tests assert on (loop actually iterated / memory ceiling honoured).
    """

    def __init__(self, body: bytes, *, chunk_size: int | None = None) -> None:
        if chunk_size is None:
            self._chunks = [body] if body else []
        else:
            self._chunks = [
                body[offset : offset + chunk_size]
                for offset in range(0, len(body), chunk_size)
            ]
        self.read_calls = 0
        self.bytes_served = 0

    async def read(self, limit: int = -1) -> bytes:
        """Return at most ``limit`` bytes of the next chunk (``b""`` at EOF)."""

        self.read_calls += 1
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if 0 <= limit < len(chunk):
            self._chunks[0] = chunk[limit:]
            chunk = chunk[:limit]
        else:
            self._chunks.pop(0)
        self.bytes_served += len(chunk)
        return chunk


class _FakeResponse:
    """Canned aiohttp response: status, content type and body only."""

    def __init__(
        self,
        status: int,
        *,
        body: bytes = b"{}",
        content_type: str = "application/json",
        chunk_size: int | None = None,
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.content = _FakeContent(body, chunk_size=chunk_size)


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


def _json_response(
    payload: Any, *, status: int = 200, chunk_size: int | None = None
) -> _FakeResponse:
    """Build a JSON ``_FakeResponse`` from a payload object."""

    return _FakeResponse(
        status, body=json.dumps(payload).encode(), chunk_size=chunk_size
    )


def _ack_deleted_response(*, status: int = 200) -> _FakeResponse:
    """Build the server's success answer for ``POST /ack``.

    Mirrors ``docker-login/token_server.py``: ``200 {"status": "deleted"}`` when
    the file was removed now, ``410 {"status": "already_deleted"}`` when it was
    already gone (duplicate ack / TTL fallback).
    """

    marker = "deleted" if status == 200 else "already_deleted"
    return _json_response({"status": marker}, status=status)


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
    # A fetch-path 410 says nothing about the file: the server is one-shot for
    # *handing out* the bundle, and its TTL delete is still ahead.
    assert excinfo.value.secret_retained is False
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
    assert err.secret_retained is False
    assert str(err) == "nope"


@pytest.mark.parametrize(
    "factory",
    [
        container_login.ContainerLoginError,
        container_login.ContainerAuthError,
        container_login.ContainerUnreachableError,
        container_login.ContainerTimeoutError,
    ],
)
async def test_secret_retained_defaults_to_false_on_every_error(
    factory: type[container_login.ContainerLoginError],
) -> None:
    """``secret_retained`` reads "not known to be retained", so it defaults off.

    The flag lives on the base class because it states a *fact* about the
    container rather than a cause, but every class must keep the conservative
    default: only :func:`container_login._require_ack_deleted` has evidence, and
    a wrongly defaulted ``True`` would tell operators to hand-delete files that
    the container removes by itself.
    """

    err = factory("boom")

    assert err.secret_retained is False
    assert str(err) == "boom"


@pytest.mark.parametrize("status", [200, 410])
async def test_ack_treats_200_and_confirmed_410_as_success(status: int) -> None:
    """ACK: a *confirmed* 410 ("already deleted") succeeds just like 200.

    This is the other half of the 410 asymmetry: inverting ``gone_is_auth``
    here would turn a completed delete into a spurious auth failure. The body
    has to carry the confirmation -- see the lockout test below for the 410 that
    must NOT be accepted.
    """

    session = _FakeSession(_ack_deleted_response(status=status))

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
    # Same class as the lockout, opposite fact: a rejected ack leaves the
    # endpoint running and its TTL delete intact, so nothing is retained on
    # purpose. Keying a "delete the file yourself" hint on the class alone
    # would fire here (see
    # ``test_only_a_proven_lockout_claims_a_retained_secret``).
    assert excinfo.value.secret_retained is False


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


# --- F-C1: a body split across several TCP chunks (read-to-EOF) -------------


async def test_fetch_reassembles_a_body_that_arrives_in_several_chunks() -> None:
    """A chunked reply must be drained to EOF, not truncated to the first read.

    ``StreamReader.read(n)`` returns whatever is buffered *now*, so a single
    call silently yields partial JSON on a multi-chunk response and turns a
    perfectly valid handoff into an intermittent ``container_unreachable``. The
    body here is deliberately delivered in small chunks; only a read loop can
    reassemble it.
    """

    bundle = _valid_bundle()
    response = _json_response(
        {"bundle": bundle, "delete_token": _DELETE_TOKEN}, chunk_size=8
    )
    session = _FakeSession(response)

    result = await _fetch(session)

    assert result == (bundle, _DELETE_TOKEN)
    # More than one read plus the EOF read: the loop really iterated.
    assert response.content.read_calls > 2


async def test_ack_reassembles_a_chunked_410_confirmation() -> None:
    """The ack path reads the same hardened way (one reader, no DRY leak)."""

    response = _json_response({"status": "already_deleted"}, status=410, chunk_size=4)
    session = _FakeSession(response)

    await _ack(session)

    assert response.content.read_calls > 2


async def test_fetch_rejects_an_oversized_body_that_arrives_in_chunks() -> None:
    """The hard ceiling survives the read loop, and buffering stays bounded.

    Chunked delivery must not become a way past the cap, and the loop must stop
    one byte past it instead of swallowing the whole stream.
    """

    oversized = b"x" * (CONTAINER_MAX_RESPONSE_BYTES * 2)
    response = _FakeResponse(200, body=oversized, chunk_size=64 * 1024)
    session = _FakeSession(response)

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert str(CONTAINER_MAX_RESPONSE_BYTES) in str(excinfo.value)
    # Never buffered more than the ceiling plus the one proving byte.
    assert response.content.bytes_served <= CONTAINER_MAX_RESPONSE_BYTES + 1


async def test_fetch_rejects_a_body_one_byte_over_the_cap() -> None:
    """The off-by-one boundary: cap plus one byte is already too much."""

    response = _FakeResponse(200, body=b"x" * (CONTAINER_MAX_RESPONSE_BYTES + 1))
    session = _FakeSession(response)

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _fetch(session)

    assert str(CONTAINER_MAX_RESPONSE_BYTES) in str(excinfo.value)


async def test_fetch_accepts_a_body_of_exactly_the_size_cap() -> None:
    """The boundary is inclusive: exactly the cap is still a valid response."""

    bundle = _valid_bundle()
    payload = {"bundle": bundle, "delete_token": _DELETE_TOKEN}
    body = json.dumps(payload).encode()
    # Pad the bundle with filler until the encoded body hits the cap exactly.
    padding = CONTAINER_MAX_RESPONSE_BYTES - len(body) - len(', "pad": ""')
    bundle["pad"] = "p" * padding
    body = json.dumps({"bundle": bundle, "delete_token": _DELETE_TOKEN}).encode()
    assert len(body) == CONTAINER_MAX_RESPONSE_BYTES

    session = _FakeSession(_FakeResponse(200, body=body, chunk_size=64 * 1024))

    result = await _fetch(session)

    assert result == (bundle, _DELETE_TOKEN)


# --- F-C2: the ack outcome is payload-conditional (lockout vs. deleted) -----


async def test_ack_410_lockout_is_an_error_not_a_success() -> None:
    """A lockout 410 keeps the secret on disk and must surface as a failure.

    ``token_server.py`` answers ``410 {"error": "locked"}`` once any loopback
    client burned the five-attempt budget -- and ``_run()`` deliberately does
    NOT delete ``secrets.json`` after a lockout. Accepting that as "already
    deleted" would retire the cleanup while the credential stays on disk
    indefinitely.
    """

    session = _FakeSession(_json_response({"error": "locked"}, status=410))

    with pytest.raises(container_login.ContainerAuthError) as excinfo:
        await _ack(session)

    assert "lock" in str(excinfo.value).lower()
    # The user is long past code entry here; the flag stays a fetch-path signal.
    assert excinfo.value.code_used is False
    # The one place in the client that has proof the file survives: this is the
    # discriminator the config flow's "delete it yourself" hint keys on.
    assert excinfo.value.secret_retained is True


@pytest.mark.parametrize("status", [200, 410])
@pytest.mark.parametrize(
    "response_factory",
    [
        # Unknown JSON object: neither marker present.
        lambda status: _json_response({"status": "something_else"}, status=status),
        # Right shape, wrong place: a JSON array carries no verdict.
        lambda status: _json_response(["already_deleted"], status=status),
        # Not JSON at all.
        lambda status: _FakeResponse(status, body=b"{not json"),
        # A proxy/foreign server answering in HTML.
        lambda status: _FakeResponse(
            status, body=b"<html>gone</html>", content_type="text/html"
        ),
        # Empty body: no confirmation either.
        lambda status: _FakeResponse(status, body=b""),
    ],
)
async def test_ack_without_a_classifiable_body_is_not_a_success(
    response_factory: Any, status: int
) -> None:
    """An unclassifiable body is no proof of deletion: explicit failure.

    The negative path is defined rather than left to fall through: anything that
    does not carry a deletion marker (and is not the known lockout refusal)
    counts as an unconfirmed delete. Both accepted statuses are held to the same
    standard -- scrutinising only the ambiguous 410 would still let a foreign
    ``200`` (a proxy, a wrong port) book a delete that never happened.
    """

    session = _FakeSession(response_factory(status))

    with pytest.raises(container_login.ContainerUnreachableError) as excinfo:
        await _ack(session)

    # Unconfirmed is not the same as retained: an unreadable answer leaves the
    # container's state unknown, and the TTL fallback is still expected to run.
    assert excinfo.value.secret_retained is False


# --- F-C3: both findings through the real production paths ------------------


def _install_real_client_with_session(
    monkeypatch: pytest.MonkeyPatch, session: _FakeSession
) -> None:
    """Wire the REAL container client into ``config_flow`` over ``session``.

    Only the aiohttp session and the token probe are replaced, so the flow runs
    through the genuine ``container_login`` primitives (status mapping, size
    cap, chunked read, 410 classification) instead of a stub.
    """

    async def _fake_pick(
        hass: Any,
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        return candidates[0][1] if candidates else None

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)
    monkeypatch.setattr(config_flow, "async_get_clientsession", lambda hass: session)


async def test_container_login_step_survives_a_chunked_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (F-C1): the flow step itself must not fail on a chunked reply.

    Drives ``async_step_container_login`` with the REAL fetch primitive; only
    the session and the token probe are faked. Before the read loop this ended
    in ``container_unreachable`` whenever the container's reply happened to
    arrive in more than one chunk.
    """

    bundle = _valid_bundle()
    session = _FakeSession(
        _json_response({"bundle": bundle, "delete_token": _DELETE_TOKEN}, chunk_size=16)
    )
    _install_real_client_with_session(monkeypatch, session)

    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}

    captured: dict[str, Any] = {}

    async def _fake_device_selection() -> dict[str, Any]:
        captured["auth_data"] = dict(flow._auth_data)
        return {"type": "form", "step_id": "device_selection"}

    flow.async_step_device_selection = _fake_device_selection  # type: ignore[assignment]

    result = await _drive_container_login(flow, pairing_code=_PAIRING_CODE)

    assert isinstance(result, dict)
    assert result.get("errors") is None or result.get("errors") == {}
    assert captured["auth_data"][CONF_OAUTH_TOKEN] == _TOKEN
    assert captured["auth_data"][CONF_GOOGLE_EMAIL] == _EMAIL


async def test_cleanup_runner_reports_a_lockout_410_as_a_failed_ack(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end (F-C2): the deferred runner must see the lockout, not silence.

    ``_async_execute_container_cleanup`` runs the REAL ``ack_consumed`` here.
    A lockout 410 leaves the credential on disk, so the failure has to reach the
    runner (warning, never the nonce/delete token) instead of the flow quietly
    booking the delete as done. Note what this does *not* claim: the runner pops
    its jobs before executing them and swallows every ``ContainerLoginError``,
    so the staged job is still consumed -- the client only refuses to call an
    unconfirmed delete a success, the retry/retention policy belongs to the
    caller (``config_flow.py``).
    """

    session = _FakeSession(_json_response({"error": "locked"}, status=410))
    _install_real_client_with_session(monkeypatch, session)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-lockout-410",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
                # Past the TTL: even that must not downgrade a lockout to debug,
                # because the lockout is exactly the case where the container
                # keeps the file.
                fetched_monotonic=time.monotonic() - (CONTAINER_TOKEN_TTL + 1),
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=config_flow._LOGGER.name):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    assert "ContainerAuthError" in caplog.text
    assert [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert _PAIRING_CODE not in caplog.text
    assert _DELETE_TOKEN not in caplog.text


@pytest.mark.parametrize(
    ("response_factory", "retained"),
    [
        # The proven lockout: token_server answers 410 {"error": "locked"} and
        # then keeps secrets.json on purpose (see token_server.py::_run).
        (lambda: _json_response({"error": "locked"}, status=410), True),
        # The look-alike: 403 {"error": "forbidden"} is the SAME error class but
        # the opposite fact. token_server returns it for a wrong delete_token
        # and for a nonce that no longer matches -- the realistic trigger being
        # a deferred ack (retained across ConfigEntryNotReady retries) that
        # reaches a *restarted* container. That instance deletes on its own TTL,
        # so telling the operator to remove the file by hand would be fiction.
        (lambda: _json_response({"error": "forbidden"}, status=403), False),
    ],
    ids=["lockout_410", "forbidden_403"],
)
async def test_only_a_proven_lockout_claims_a_retained_secret(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response_factory: Any,
    retained: bool,
) -> None:
    """The "container kept the file" message is keyed on the fact, not the class.

    Both answers below raise ``ContainerAuthError`` out of the REAL client, so a
    branch that keys on the error *type* cannot tell them apart and would invent
    a lockout for the 403. Only ``_require_ack_deleted`` has the evidence (the
    lockout body) and marks it with ``secret_retained``.
    """

    session = _FakeSession(response_factory())
    _install_real_client_with_session(monkeypatch, session)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-retained-secret",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
                # Inside the TTL for both cases, so the debug branch cannot
                # absorb either of them and the two warnings are comparable.
                fetched_monotonic=time.monotonic(),
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=config_flow._LOGGER.name):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert warnings, "an unconfirmed delete is a problem on either path"
    text = "\n".join(record.getMessage() for record in warnings)
    assert "ContainerAuthError" in text
    if retained:
        assert "kept its secrets.json" in text
        assert "manually" in text
        # No false promise of an automatic cleanup: none follows a lockout.
        assert "TTL delete" not in text
    else:
        # The container is still up and its TTL still deletes, so the generic
        # message is the truthful one -- and none of the lockout wording may
        # leak into it.
        assert "TTL delete" in text
        assert "lockout" not in text
        assert "manually" not in text
    assert _PAIRING_CODE not in caplog.text
    assert _DELETE_TOKEN not in caplog.text


async def test_cleanup_runner_stays_quiet_for_a_confirmed_410(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The counter-test: the idempotent 410 stays a silent success.

    Guards the reverse mutation -- turning *every* ack-side 410 into an error
    would spam a warning on the perfectly normal duplicate-ack / TTL-fallback
    ending.
    """

    session = _FakeSession(_ack_deleted_response(status=410))
    _install_real_client_with_session(monkeypatch, session)

    hass = _build_hass([])
    config_flow._async_stage_container_cleanup(
        hass,
        flow_id="flow-confirmed-410",
        unique_id=_EMAIL,
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
    )

    with caplog.at_level(logging.DEBUG, logger=config_flow._LOGGER.name):
        await _run_staged_cleanup(hass, unique_id=_EMAIL)

    warnings = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert warnings == []
    assert len(session.calls) == 1


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


async def test_reauth_rejects_manual_token_combined_with_a_pairing_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disabled manual-token field still counts as a credential method.

    Guards the ``_REAUTH_FIELD_TOKEN`` entry of ``_REAUTH_CREDENTIAL_FIELDS``,
    which the secrets+code test cannot reach: drop that entry and this
    submission counts as ONE method, so the flow would fetch and burn the
    one-shot pairing code while ignoring the token. The UI input is currently
    commented out, but a hand-crafted or restored submission still arrives here.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = make_config_entry(
        entry_id="entry-reauth-token-plus-code",
        data={CONF_GOOGLE_EMAIL: _EMAIL},
        options={},
    )
    hass = _build_hass([entry])
    captured: dict[str, Any] = {}
    flow = _make_reauth_flow(hass, entry, captured)

    result = await _maybe_await(
        flow.async_step_reauth_confirm(
            {
                "new_oauth_token": "aas_et/manual-token-value",
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "choose_one"}
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []
    assert "data" not in captured


async def test_options_rejects_manual_token_combined_with_a_pairing_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Options pendant: guards ``new_oauth_token`` in ``_OPTIONS_CREDENTIAL_FIELDS``."""

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)

    entry = _options_entry("entry-options-token-plus-code")
    hass = _build_hass([entry])
    flow = _make_options_flow(hass, entry)

    result = await _maybe_await(
        flow.async_step_credentials(
            {
                "new_oauth_token": "aas_et/manual-token-value",
                "container_host": "127.0.0.1",
                "container_port": CONTAINER_TOKEN_PORT,
                "pairing_code": _PAIRING_CODE,
            }
        )
    )
    assert isinstance(result, dict)
    assert result.get("type") == "form"
    assert result.get("errors") == {"base": "choose_one"}
    assert recorder.fetch_calls == []
    assert recorder.ack_calls == []
    assert DATA_SECRET_BUNDLE not in entry.data


async def test_novnc_access_placeholder_brackets_ipv6_exactly_once() -> None:
    """An IPv6 literal must render as ``http://[addr]:7900`` in both spellings.

    ``_classify_novnc_host`` strips brackets before parsing, so it accepts the
    bare and the bracketed form alike. Interpolating the raw input would emit
    ``http://2001:db8::1:7900``, which no browser can parse, and adding brackets
    unconditionally would double them for the already-bracketed spelling.
    """

    expected = "http://[2001:db8::1]:7900"
    for spelling in ("2001:db8::1", "[2001:db8::1]", " 2001:db8::1 "):
        rendered = config_flow._novnc_access_placeholder(spelling)
        assert rendered == f"[{expected}]({expected})", spelling

    # Loopback IPv6 stays unlinked, like its IPv4 counterpart.
    assert "](http" not in config_flow._novnc_access_placeholder("::1")


async def test_build_base_url_brackets_ipv6_exactly_once() -> None:
    """The token endpoint URL must survive a bare IPv6 literal in the host field.

    ``host`` is free text in the form and the flow accepts both IPv6 spellings,
    so a bare ``::1`` interpolated raw yields ``http://::1:7901`` -- an authority
    in which the port cannot be told from the address, making every fetch and
    every acknowledgement fail as "unreachable". Bracketing unconditionally
    would instead double the brackets of the already-bracketed spelling.
    """

    assert container_login._build_base_url("::1", 7901) == "http://[::1]:7901"
    assert container_login._build_base_url("[::1]", 7901) == "http://[::1]:7901"
    assert (
        container_login._build_base_url("2001:db8::1", 7901)
        == "http://[2001:db8::1]:7901"
    )
    # IPv4 and host names must stay byte-identical to the previous behaviour.
    assert container_login._build_base_url("127.0.0.1", 7901) == "http://127.0.0.1:7901"
    assert (
        container_login._build_base_url("googlefindmy-login", 7901)
        == "http://googlefindmy-login:7901"
    )


async def test_link_local_guard_sees_the_same_host_as_the_url_builder() -> None:
    """Guard and URL builder must read the host field through one normalisation.

    Regression: while ``_build_base_url`` learned to trim and unbracket, the
    guard still parsed the raw text, so ``[169.254.169.254]`` was "not a literal
    IP" to the guard while the builder produced
    ``http://169.254.169.254:7901``. Before that change the malformed authority
    ``http://[169.254.169.254]:7901`` had made yarl refuse the request, so the
    hole was newly opened by the fix, not merely uncovered by it.

    IPv6 link-local is asserted for the same reason: it used to be unreachable
    by that same accident, so the rule now has to be stated rather than relied
    upon.
    """

    for spelling in (
        "169.254.169.254",
        "[169.254.169.254]",
        " 169.254.169.254 ",
        "fe80::1",
        "[fe80::1]",
    ):
        with pytest.raises(container_login.ContainerUnreachableError):
            container_login._maybe_block_link_local(spelling)

    # Ordinary targets must stay reachable in every spelling.
    for spelling in ("127.0.0.1", "::1", "[::1]", "192.168.1.21", "googlefindmy-login"):
        container_login._maybe_block_link_local(spelling)


async def test_host_syntax_guard_validates_the_string_the_url_builder_uses() -> None:
    """The guard must not validate a friendlier normalisation of the host.

    Same class as the bracket regression above, one layer down: the guard
    stripped the trailing dots with ``str.rstrip(".")``, which removes a whole
    RUN of them, and then validated the result. ``a.b..`` therefore passed as
    the well-formed ``a.b``, while ``_build_base_url`` interpolates the value it
    was given and emits ``http://a.b..:7901``. Guard and request disagreed about
    the same input.

    A single trailing dot is the legal absolute-DNS spelling and must stay
    accepted, so the fix trims exactly one, never a run. Asserting the URL too
    keeps this a statement about the two components together rather than about
    the guard alone.
    """

    for malformed in ("a.b..", "a.b...", "example.com..", "a..b"):
        with pytest.raises(container_login.ContainerUnreachableError):
            container_login._reject_non_host_syntax(malformed)

    # Legal spellings, including the absolute-DNS form with ONE trailing dot.
    for legal in ("example.com.", "example.com", "host", "googlefindmy-login"):
        container_login._reject_non_host_syntax(legal)

    # The reason the guard has to be strict: the builder does not re-normalise
    # the dots away, so anything the guard lets through reaches yarl verbatim.
    assert container_login._build_base_url("a.b..", 7901) == "http://a.b..:7901"


async def test_host_normalisation_is_shared_between_client_and_config_flow() -> None:
    """One helper, so classification and request can never disagree.

    ``config_flow`` previously used ``strip("[]")``, which also eats unbalanced
    or repeated brackets; the client stripped exactly one pair. A host the flow
    called "linkable" could therefore be a host the client refused to build a
    URL for.
    """

    assert config_flow.normalise_host_literal is container_login.normalise_host_literal
    assert container_login.normalise_host_literal("[::1]") == "::1"
    assert container_login.normalise_host_literal("  192.168.1.21 ") == "192.168.1.21"
    # Exactly one pair: an unbalanced or doubled spelling stays malformed and is
    # therefore classified as a host name rather than silently repaired.
    assert container_login.normalise_host_literal("[[::1]]") == "[::1]"
    assert config_flow._classify_novnc_host("[::1") == "hostname"


async def test_link_local_guard_rejects_residual_brackets() -> None:
    """Nested or unbalanced brackets must not become a way around the guard.

    ``normalise_host_literal`` strips exactly ONE pair, so ``[[fe80::1]]``
    becomes ``[fe80::1]``. That is not an IP literal, so the guard would have
    fallen through to its host-name branch and stayed silent, while
    ``_build_base_url`` produced ``http://[fe80::1]:7901`` -- an authority
    ``yarl`` accepts. The bearer nonce would then have gone to a link-local
    target the guard exists to refuse.
    """

    for spelling in ("[[fe80::1]]", "[[169.254.169.254]]", "[::1", "]::1["):
        with pytest.raises(container_login.ContainerUnreachableError):
            container_login._maybe_block_link_local(spelling)

    # The one legal bracket pair must still pass.
    container_login._maybe_block_link_local("[::1]")


async def test_guard_rejects_url_syntax_smuggled_into_the_host_field() -> None:
    """A host that is not a bare host component must never reach the URL.

    ``host`` is free text and is interpolated into the base URL, so an authority
    or path delimiter silently retargets the request. Reproduced before fixing:

    ==========================  ==========================  ==================
    host                        resulting URL               yarl parses as
    ==========================  ==========================  ==================
    ``169.254.169.254#x``       ``http://169.254.169.254#x:7901``  host ``169.254.169.254``, port 80
    ``user@127.0.0.1``          ``http://user@127.0.0.1:7901``     URL credentials
    ==========================  ==========================  ==================

    The first reaches the very link-local target this guard exists to refuse
    (the ``#`` starts a fragment, so ``:7901`` never becomes the port); the
    second makes aiohttp raise an uncaught ``ValueError``, because URL
    credentials cannot be combined with the explicit ``Authorization`` header.
    Neither parses as an IP literal, so an IP-only guard stayed silent.
    """

    for smuggled in (
        "169.254.169.254#x",
        "user@127.0.0.1",
        "127.0.0.1?a=b",
        "127.0.0.1/x",
        "127.0.0.1:8080",
        "-leading-hyphen",
        "a" * 300,
        "",
    ):
        with pytest.raises(container_login.ContainerUnreachableError):
            container_login._maybe_block_link_local(smuggled)

    # Ordinary host names and IP literals must keep working.
    for ok in (
        "googlefindmy-login",
        "my.host.local",
        "host.local.",
        "127.0.0.1",
        "[::1]",
    ):
        container_login._maybe_block_link_local(ok)


def _stage_entry_ticket(hass: Any, *, flow_id: str, entry: Any) -> None:
    """Stage one ack job on an entry-correlated ticket, as an update path does."""

    config_flow._async_stage_container_cleanup_for(
        hass,
        flow_id=flow_id,
        unique_id=getattr(entry, "unique_id", None),
        job=config_flow.PendingContainerCleanup(
            ack=config_flow._ContainerAckTarget(
                host="127.0.0.1",
                port=CONTAINER_TOKEN_PORT,
                pairing_code=_PAIRING_CODE,
                delete_token=_DELETE_TOKEN,
            )
        ),
        entry=entry,
    )


async def test_entry_removal_spares_a_concurrent_flows_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing an entry must not discard another running flow's ticket.

    The claim helper falls back from the entry id to the account and then to any
    account-less ticket, because a create flow cannot know its entry id yet.
    Reusing that selection for a removal would let entry A discard the ticket of
    a concurrent same-account flow B, leaving B's watched bundle undeleted and
    its container un-acked. Removal is therefore addressed by entry id only.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    doomed = make_config_entry(entry_id="entry-doomed", unique_id=_EMAIL)
    _stage_entry_ticket(hass, flow_id="flow-update", entry=doomed)
    # A second ticket for the same entry, carrying TWO jobs. It pins the return
    # contract on the job count rather than the ticket count: a body counting
    # tickets would report 2 here instead of 3.
    _stage_entry_ticket(hass, flow_id="flow-update-2", entry=doomed)
    _stage_entry_ticket(hass, flow_id="flow-update-2", entry=doomed)
    # A different flow, same account, still running: its ticket carries no entry
    # id and is exactly what the account fallback would have swallowed.
    _stage_ack_ticket(hass, flow_id="flow-concurrent", unique_id=_EMAIL)

    discarded = config_flow.async_discard_pending_container_cleanup_for_entry(
        hass, entry_id="entry-doomed"
    )

    assert discarded == 3
    assert [ticket.flow_id for ticket in _staged_cleanup(hass)] == ["flow-concurrent"]

    # A caller without an entry id must drop nothing. Only ``None`` discriminates
    # here: an unguarded ``ticket.entry_id == None`` would match exactly the
    # surviving uncorrelated ticket. (``""`` cannot reach a ticket at all, the
    # staging helper normalises it away via ``entry_id or None``.)
    assert (
        config_flow.async_discard_pending_container_cleanup_for_entry(
            hass, entry_id=None
        )
        == 0
    )
    assert [ticket.flow_id for ticket in _staged_cleanup(hass)] == ["flow-concurrent"]


async def test_entry_removal_drains_every_ticket_of_that_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All of a removed entry's tickets go in one pass, with no upper bound.

    Each update path stages its own ticket, so an entry can accumulate many
    before their reloads claim them. A per-call cutoff would strand exactly the
    tickets this drain exists to clear: they can never be claimed once the entry
    is gone and would hold pairing nonces and delete tokens in ``hass.data`` for
    the rest of the process lifetime.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    doomed = make_config_entry(entry_id="entry-many", unique_id=_EMAIL)
    # Deliberately above the cutoff this fix removed (_MAX_CLEANUP_TICKETS_PER_ENTRY
    # was 16). The constant is gone, so the number is no longer grep-able from the
    # source; it is named here so a reintroduced bound of any size below this is
    # caught rather than silently passing.
    staged_count = 20
    for index in range(staged_count):
        _stage_entry_ticket(hass, flow_id=f"flow-update-{index}", entry=doomed)
    assert len(_staged_cleanup(hass)) == staged_count

    discarded = config_flow.async_discard_pending_container_cleanup_for_entry(
        hass, entry_id="entry-many"
    )

    assert discarded == staged_count
    assert _staged_cleanup(hass) == []
    # Nothing was executed: discarding keeps the credentials on disk.
    assert recorder.ack_calls == []

    # The drain emptied the staging area, so the bucket key is gone. A second
    # removal must report zero rather than raise -- entry removal runs on paths
    # that never staged anything at all.
    assert (
        config_flow.async_discard_pending_container_cleanup_for_entry(
            hass, entry_id="entry-many"
        )
        == 0
    )


async def test_entry_drain_before_claim_discard_clears_both_ticket_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate-account abort needs BOTH discards, drain first.

    That path (``__init__.py``, ``should_setup`` false) leaves the entry in
    place but must let go of everything it staged. The entry can hold several
    update-path tickets *and* the uncorrelated create-path ticket of the flow
    that just produced it. The claim-based discard takes at most one ticket and
    prefers an ``entry_id`` match, so running it first would consume one update
    ticket and strand the create-path one. Drain first, claim second.
    """

    recorder = _Recorder()
    _install_container_client(monkeypatch, recorder)
    hass = _build_hass([])

    entry = make_config_entry(entry_id="entry-dup", unique_id=_EMAIL)
    _stage_entry_ticket(hass, flow_id="flow-update-a", entry=entry)
    _stage_entry_ticket(hass, flow_id="flow-update-b", entry=entry)
    _stage_ack_ticket(hass, flow_id="flow-create", unique_id=_EMAIL)

    discarded = config_flow.async_discard_pending_container_cleanup_for_entry(
        hass, entry_id="entry-dup"
    )
    discarded += config_flow.async_discard_pending_container_cleanup(
        hass, unique_id=_EMAIL, entry_id="entry-dup"
    )

    assert discarded == 3
    assert _staged_cleanup(hass) == []
    # Discarding never executes: the credential files stay on disk.
    assert recorder.ack_calls == []


async def test_discovery_create_branch_marks_its_own_late_staged_ticket() -> None:
    """The post-CREATE_ENTRY staging in async_step_discovery must mark too.

    ``async_step_discovery`` stages its delete-after-import job *after*
    ``async_step_device_selection`` returned ``CREATE_ENTRY``, i.e. after the
    create path already set the promise marker. If that late staging created the
    flow's ticket (the discovery flow can reach CREATE_ENTRY without having
    staged anything before), the earlier mark applied to nothing and the ticket
    is born unmarked. Home Assistant removes the flow immediately afterwards,
    ``_async_discard_cleanup_ticket_for_flow`` sees an unmarked, uncorrelated
    ticket and drops it -- so an entry whose first ``async_setup_entry`` retries
    loses the delete-after-import job it was promised.

    Drives the real ``async_step_discovery`` confirm branch and replaces only
    ``async_step_device_selection`` with its CREATE_ENTRY FlowResult, because
    that is the input the branch reacts to.
    """

    hass = _build_hass([])
    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "discovery"}
    flow.flow_id = "flow-discovery"  # type: ignore[attr-defined]

    payload = config_flow.CloudDiscoveryData(
        email=_EMAIL,
        unique_id=_EMAIL,
        candidates=((_EMAIL, _TOKEN),),
        secrets_bundle=_valid_bundle(),
    )
    flow._discovery_confirm_pending = True  # type: ignore[attr-defined]
    flow._pending_discovery_payload = payload  # type: ignore[attr-defined]
    flow._pending_discovery_updates = None  # type: ignore[attr-defined]
    flow._pending_discovery_existing_entry = None  # type: ignore[attr-defined]

    async def _created() -> Any:
        # Mirrors what the create path does right before returning: mark, then
        # hand the CREATE_ENTRY result back to async_step_discovery.
        flow._async_mark_own_cleanup_ticket_entry_promised()
        return {"type": config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY}

    flow.async_step_device_selection = _created  # type: ignore[assignment,method-assign]

    result = await _maybe_await(flow.async_step_discovery(None))
    assert result["type"] == config_flow.data_entry_flow.FlowResultType.CREATE_ENTRY

    staged = _staged_cleanup(hass)
    assert len(staged) == 1, "the discovery create branch should stage its job"
    assert staged[0].entry_promised is True, (
        "the job staged after CREATE_ENTRY must be marked at its own site"
    )

    # The consequence the marker exists for: the flow removal that Home
    # Assistant performs next must leave the ticket alone.
    flow.async_remove()
    assert len(_staged_cleanup(hass)) == 1

# tests/test_token_server.py
"""Behavioural tests for the standalone container-login token endpoint.

Target under test: ``custom_components/googlefindmy/docker-login/token_server.py``.
That module runs *inside the login container* and imports **nothing** from the
integration package; it only needs :mod:`aiohttp`. These tests therefore drive
the real ``aiohttp`` application built by ``_build_app`` through an in-process
:class:`aiohttp.test_utils.TestServer` / :class:`aiohttp.test_utils.TestClient`,
exercising the security contract documented in the module docstring:

* Two-phase delete: ``GET /secrets`` returns the bundle + ``delete_token`` and
  leaves the file on disk; ``POST /ack`` with a matching token deletes it and
  shuts the endpoint down; a second ``GET`` then reports ``410 gone``.
* One-shot delivery: once a ``GET`` succeeded, every further ``GET`` is refused
  with ``410`` even while the file is still on disk, and the ``POST /ack`` of the
  original client keeps working.
* Nonce lockout: a wrong bearer nonce yields ``403`` and, after
  ``_NONCE_MAX_ATTEMPTS`` failures, flips to ``410`` + shutdown. The counter is
  server-wide: a wrong nonce on ``POST /ack`` feeds the same budget, and a
  locked endpoint refuses both routes.
* Non-ASCII credentials: a bearer nonce or a ``delete_token`` carrying non-ASCII
  code points (including a lone surrogate from a JSON ``\\ud800`` escape) is an
  ordinary mismatch, not a ``TypeError`` out of ``hmac.compare_digest``. It must
  yield the regular rejection status *and*, for the nonce, increment the lockout
  counter, otherwise the lockout can be bypassed with 500s.
* Body hardening: an oversized ``/ack`` body is rejected with ``413`` and a
  non-JSON body with ``400``; neither burns the lockout counter.
* TTL fallback: the on-disk secret is deleted even when no ack arrives, both via
  a direct drive of ``_delete_secret_file`` and through the real ``_run``
  timeout path against a live loopback socket.
* Lockout is non-destructive: after a lockout ``_run`` returns (the endpoint is
  gone, the port refuses connections) but ``secrets.json`` stays on disk, so the
  file-handoff and copy/paste tracks survive an unauthenticated lockout.
* ACK idempotency: a second correct ack returns ``410`` without error, and a
  *failed* ack (wrong ``delete_token``) does not burn the nonce.
* Constant mirroring: the values copied into the standalone module are asserted
  against ``const.py``, the source of truth, not against literals.
* Host-publish loopback boundary: the container-internal bind is ``0.0.0.0`` on
  purpose (Docker bridge DNATs the published port onto eth0, not container
  loopback), and the *no-LAN* guarantee is asserted against the HOST publish,
  NOT against the container bind. Since the token port became an opt-in overlay
  (Track A/C must start even when 7901 is taken on the host), that publish lives
  in ``docker-compose.oneclick.yml``; the base ``docker-compose.yml`` must not
  publish 7901 at all. Both files are asserted, on parsed YAML rather than raw
  text, because both carry ``127.0.0.1:7901:7901`` and ``network_mode: host`` in
  explanatory comments that a text match would happily mistake for the real
  thing.

Collection hardening (Audit HOCH-3): ``docker-login/`` is NOT a Python package
and its basename is not importable via ``import``. The module is loaded with
``importlib.util.spec_from_file_location`` under a synthetic, dotted-free module
name; no ``sys.path`` entry is appended (that would provoke pytest's
``import file mismatch`` because the ``docker-login`` basename collides across
the tree). ``pytest --collect-only tests/test_token_server.py`` must stay green.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import aiohttp
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here. This module
# mixes sync tests (constant/bind guards, TTL-fallback helper drives) with async
# tests (the live TestServer round-trips). The repo runs pytest under
# ``asyncio_mode = "auto"`` (pyproject.toml), so coroutine tests are collected as
# async automatically; a blanket module mark would wrongly tag the sync tests and
# emit "marked with asyncio but not async" warnings. Async cases are marked
# individually below. No ``asyncio.run`` is used (see tests/AGENTS.md).

# Absolute path to the standalone module. ``docker-login`` is a plain directory
# (not a package), so we load the file directly instead of importing it.
_DOCKER_LOGIN_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "googlefindmy"
    / "docker-login"
)
_TOKEN_SERVER_PATH = _DOCKER_LOGIN_DIR / "token_server.py"
_COMPOSE_PATH = _DOCKER_LOGIN_DIR / "docker-compose.yml"
# The token port is an opt-in overlay: the base compose publishes noVNC only, so
# Track A (file handoff) and Track C (cleartext) still start when 7901 is busy.
_ONECLICK_COMPOSE_PATH = _DOCKER_LOGIN_DIR / "docker-compose.oneclick.yml"


def _load_token_server() -> ModuleType:
    """Load ``token_server.py`` by path without touching ``sys.path``.

    Using a synthetic module name (no ``docker-login`` basename, no dotted
    package) sidesteps pytest's ``import file mismatch`` guard and keeps the
    non-package directory out of the import machinery.
    """

    spec = importlib.util.spec_from_file_location(
        "gfmy_token_server_under_test", _TOKEN_SERVER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


token_server = _load_token_server()


_VALID_NONCE = "test-pairing-nonce-abcdef0123456789"
_BUNDLE = {"google_email": "user@example.com", "shared_key": "deadbeef"}


def _write_secret(tmp_path: Path) -> Path:
    """Write a bundle to a temp secrets.json and return its path."""

    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(_BUNDLE), encoding="utf-8")
    return secrets_path


def _make_state(tmp_path: Path) -> Any:
    """Build a ``_State`` bound to a fresh on-disk secret."""

    return token_server._State(_VALID_NONCE, _write_secret(tmp_path))


async def _client(state: Any) -> TestClient:
    """Start the real app for ``state`` and return a connected test client."""

    app = token_server._build_app(state)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


def _auth(nonce: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {nonce}"}


def _free_loopback_port() -> int:
    """Reserve and release an ephemeral loopback port for the ``_run`` drives."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_until_listening(port: int) -> None:
    """Block until ``_run`` has its site up on ``port`` (or fail the test)."""

    for _ in range(250):
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.02)
        else:
            writer.close()
            await writer.wait_closed()
            return
    raise AssertionError(f"token server never started listening on {port}")


def _patch_run_socket(monkeypatch: pytest.MonkeyPatch, port: int) -> list[Any]:
    """Point ``_run`` at a private loopback port and capture its ``_State``.

    ``_run`` builds the state itself, so the test grabs it by wrapping
    ``_build_app``. The bind is forced to ``127.0.0.1``: the production
    ``0.0.0.0`` bind is correct inside the container (bridge DNAT) but must not
    be opened on a developer/CI machine by a test.
    """

    captured: list[Any] = []
    real_build_app = token_server._build_app

    def _capture(state: Any) -> Any:
        captured.append(state)
        return real_build_app(state)

    monkeypatch.setattr(token_server, "_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(token_server, "_TOKEN_PORT", port)
    monkeypatch.setattr(token_server, "_build_app", _capture)
    return captured


# ---------------------------------------------------------------------------
# Collection hardening / bind contract
# ---------------------------------------------------------------------------


def test_module_loads_and_mirrors_constants() -> None:
    """The module loads by path and mirrors the integration const values.

    ``token_server.py`` runs without the integration package on its path, so it
    keeps private copies of the container-login constants that are kept in sync
    with ``const.py`` *by convention* (module docstring). The convention is only
    worth anything if it is asserted against the source of truth: comparing the
    copies with literals would let a change to ``const.py`` drift past a green
    test. Every mirrored value is therefore checked against ``const``.
    """

    # Imported inside the test on purpose: the module under test is standalone
    # and must not need the integration package at import/collection time.
    from custom_components.googlefindmy import const

    assert token_server._TOKEN_PORT == const.CONTAINER_TOKEN_PORT
    assert token_server._NONCE_BYTES == const.CONTAINER_NONCE_MIN_LEN
    assert token_server._NONCE_MAX_ATTEMPTS == const.CONTAINER_NONCE_MAX_ATTEMPTS
    assert token_server._TOKEN_TTL == const.CONTAINER_TOKEN_TTL
    assert token_server._MAX_BODY_BYTES == const.CONTAINER_MAX_RESPONSE_BYTES

    # Not mirrored from const.py: the container-internal bind is 0.0.0.0 on
    # purpose (bridge DNAT); the loopback boundary is the host publish, asserted
    # in the compose test. The port literal anchors the third corner of the
    # const/module/compose triangle.
    assert token_server._BIND_HOST == "0.0.0.0"
    assert token_server._TOKEN_PORT == 7901


def test_container_bind_is_wildcard_for_bridge_dnat() -> None:
    """The in-container bind is ``0.0.0.0`` so the bridge-DNAT'd publish is reachable.

    This is deliberate: under Docker's default bridge network the published host
    port lands on the container's eth0, not its loopback, so a container-loopback
    bind would make 127.0.0.1:7901 unreachable. The no-LAN boundary is asserted
    separately against the HOST publish (see below), NOT against this bind.
    """

    assert token_server._BIND_HOST == "0.0.0.0"


def _published_ports(path: Path) -> list[str]:
    """Return every ``ports:`` entry of every service in a compose file.

    Parsed from YAML on purpose: both compose files carry ``127.0.0.1:7901:7901``
    inside explanatory comments, so a raw-text match would pass even if the real
    publish were removed or widened.
    """

    compose = yaml.safe_load(path.read_text(encoding="utf-8"))
    services: dict[str, Any] = compose["services"]
    return [
        str(port) for service in services.values() for port in service.get("ports", [])
    ]


def test_base_compose_does_not_publish_the_token_port() -> None:
    """The base compose publishes noVNC only, never the token port.

    Publishing 7901 unconditionally made the whole container fail to start when
    the port was already taken on the host, which also broke Track A (file
    handoff) and Track C (cleartext) that never touch that port. The token port
    therefore moved into an opt-in overlay.
    """

    ports = _published_ports(_COMPOSE_PATH)
    assert [port for port in ports if "7901" in port] == []
    # noVNC stays published in every track.
    assert any("7900" in port for port in ports)


def test_host_publish_is_loopback_only_no_lan_exposure() -> None:
    """The security boundary: the overlay publishes 7901 on host loopback only.

    The no-LAN guarantee lives in the host-side publish, not in the container
    bind. The overlay must publish the token port exactly as
    ``127.0.0.1:7901:7901``: no wildcard publish (``0.0.0.0:7901``), no bare
    ``7901:7901`` (which binds all interfaces), and no variable-driven host bind
    (there is deliberately no LAN opt-in for this port, unlike noVNC 7900).
    """

    token_ports = [
        port for port in _published_ports(_ONECLICK_COMPOSE_PATH) if "7901" in port
    ]
    assert token_ports == ["127.0.0.1:7901:7901"]


@pytest.mark.parametrize("compose_path", [_COMPOSE_PATH, _ONECLICK_COMPOSE_PATH])
def test_compose_does_not_set_network_mode(compose_path: Path) -> None:
    """No service may set ``network_mode``; ``host`` would collapse the boundary.

    The two-layer guarantee is "wildcard bind inside the container, loopback
    publish on the host". ``network_mode: host`` removes the bridge and with it
    the publish, so the container's ``0.0.0.0:7901`` bind would land directly on
    the host's interfaces and expose the cleartext token endpoint to the LAN.
    Both compose files *mention* ``network_mode: host`` in a warning comment, so
    this is asserted on the parsed YAML rather than on the raw text.
    """

    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services: dict[str, Any] = compose["services"]
    offenders = sorted(
        name for name, service in services.items() if "network_mode" in service
    )
    assert offenders == []


# ---------------------------------------------------------------------------
# Two-phase delete happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_get_returns_bundle_and_leaves_file(tmp_path: Path) -> None:
    """A nonce-correct ``GET /secrets`` returns bundle + delete_token, file stays."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert resp.status == 200
        payload = await resp.json()
        assert payload["bundle"] == _BUNDLE
        assert payload["delete_token"] == state.delete_token
        # Two-phase: the file is NOT deleted by GET.
        assert state.secrets_path.is_file()
        assert state.deleted is False
        assert state.delivered is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_deletes_file_and_second_get_is_gone(tmp_path: Path) -> None:
    """A matching ``POST /ack`` deletes the file; a later ``GET`` returns 410."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        first = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert first.status == 200
        delete_token = (await first.json())["delete_token"]

        ack = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": delete_token},
        )
        assert ack.status == 200
        assert (await ack.json())["status"] == "deleted"
        assert not state.secrets_path.exists()
        assert state.deleted is True
        assert state.shutdown_event.is_set()

        second = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert second.status == 410
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_second_get_after_delivery_is_refused(tmp_path: Path) -> None:
    """One-shot delivery: a repeat ``GET /secrets`` is refused with 410.

    The two-phase delete leaves the file on disk until the ack, so the delivery
    itself has to be the one-shot gate. Without it every holder of the pairing
    code could re-collect the bundle until the ack or the TTL fired. The ack must
    stay usable afterwards, otherwise the original client could not complete the
    delete.
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        first = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert first.status == 200
        delete_token = (await first.json())["delete_token"]

        second = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert second.status == 410
        assert (await second.json())["error"] == "already_delivered"
        # Refused, not deleted: the two-phase delete is untouched by the refusal.
        assert state.deleted is False
        assert state.secrets_path.is_file()
        # The refusal is not a nonce failure and must not feed the lockout.
        assert state.failed_attempts == 0

        # The ACK path still works for the client that received the bundle.
        ack = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": delete_token},
        )
        assert ack.status == 200
        assert (await ack.json())["status"] == "deleted"
        assert not state.secrets_path.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_reports_gone_when_file_vanished(tmp_path: Path) -> None:
    """A secret removed out-of-band yields ``410 gone``, not a stack trace.

    Guards the read path of the merged terminal-state branch: ``deleted`` is
    still ``False`` here, so the 410 can only come from the ``FileNotFoundError``
    handler around the actual read.
    """

    state = _make_state(tmp_path)
    state.secrets_path.unlink()
    client = await _client(state)
    try:
        resp = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert resp.status == 410
        assert (await resp.json())["error"] == "gone"
        assert state.delivered is False
        assert state.deleted is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_reports_read_failure_without_marking_delivered(
    tmp_path: Path,
) -> None:
    """An unreadable/corrupt secrets file yields 500 and no delivery is recorded.

    ``delivered`` must stay ``False`` so a retry after fixing the file still
    works: the one-shot gate may only close on an actual handoff.
    """

    state = _make_state(tmp_path)
    state.secrets_path.write_text("{not json", encoding="utf-8")
    client = await _client(state)
    try:
        resp = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert resp.status == 500
        assert (await resp.json())["error"] == "read_failed"
        assert state.delivered is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Nonce failures + lockout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_nonce_returns_403(tmp_path: Path) -> None:
    """A single wrong bearer nonce is rejected with 403 (no lockout yet)."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.get("/secrets", headers=_auth("wrong-nonce"))
        assert resp.status == 403
        assert state.failed_attempts == 1
        assert state.locked is False
        # File untouched on a rejected read.
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_lockout_after_max_attempts(tmp_path: Path) -> None:
    """After ``_NONCE_MAX_ATTEMPTS`` wrong nonces the endpoint locks (410 + shutdown)."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        statuses = []
        for _ in range(token_server._NONCE_MAX_ATTEMPTS):
            resp = await client.get("/secrets", headers=_auth("wrong-nonce"))
            statuses.append(resp.status)
        # First N-1 are 403; the Nth flips to 410 (locked).
        assert statuses[:-1] == [403] * (token_server._NONCE_MAX_ATTEMPTS - 1)
        assert statuses[-1] == 410
        assert state.locked is True
        assert state.shutdown_event.is_set()
        assert state.shutdown_reason == token_server._SHUTDOWN_LOCKOUT

        # Even a subsequently *correct* nonce is refused once locked.
        after = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert after.status == 410
        # The secret was never handed out.
        assert state.delivered is False
        # ... and never destroyed either: the lockout closes the endpoint, it
        # does not wipe the credential (``_run`` skips the delete, see below).
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_missing_authorization_header_is_rejected(tmp_path: Path) -> None:
    """A request without any ``Authorization`` header counts as a bad nonce."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.get("/secrets")
        assert resp.status == 403
        assert state.failed_attempts == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_ascii_bearer_nonce_is_rejected_and_counted(tmp_path: Path) -> None:
    """A non-ASCII bearer nonce is a normal mismatch: 403 *and* counted.

    Regression guard: ``hmac.compare_digest`` raises ``TypeError`` on ``str``
    arguments with non-ASCII code points. HTTP header values are latin-1 on the
    wire, so ``Authorization: Bearer b\\xe4d`` is a legal request that used to
    blow up the handler with a 500 traceback while leaving ``failed_attempts``
    at 0. That made the documented lockout bypassable by unlimited guessing.
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.get("/secrets", headers=_auth("b\xe4d"))
        assert resp.status == 403
        assert (await resp.json())["error"] == "forbidden"
        assert state.failed_attempts == 1
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_ascii_nonce_still_reaches_the_lockout(tmp_path: Path) -> None:
    """Repeated non-ASCII nonces lock the endpoint like any other wrong nonce."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        statuses = [
            (await client.get("/secrets", headers=_auth("b\xe4d"))).status
            for _ in range(token_server._NONCE_MAX_ATTEMPTS)
        ]
        assert statuses[-1] == 410
        assert state.locked is True
        assert state.failed_attempts == token_server._NONCE_MAX_ATTEMPTS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_non_ascii_delete_token_is_rejected(tmp_path: Path) -> None:
    """A non-ASCII ``delete_token`` yields 403, not a 500, and deletes nothing.

    Second site of the same ``compare_digest`` class as the bearer nonce: the
    ``/ack`` body is JSON and may carry arbitrary Unicode.
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": "b\xe4d-token"},
        )
        assert resp.status == 403
        assert (await resp.json())["error"] == "forbidden"
        assert state.deleted is False
        assert state.secrets_path.is_file()
        # A wrong delete_token never feeds the lockout counter (retry safety).
        assert state.failed_attempts == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_lone_surrogate_delete_token_is_rejected(tmp_path: Path) -> None:
    """A lone surrogate in the ``delete_token`` is rejected, not a 500.

    ``json.loads`` turns the escape ``\\ud800`` into a lone surrogate, which
    plain ``utf-8`` (and ``surrogateescape``) cannot encode. The comparison
    helper therefore uses ``surrogatepass``, the only handler that is total over
    every ``str`` the JSON decoder can produce.
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers={**_auth(_VALID_NONCE), "Content-Type": "application/json"},
            data=b'{"delete_token": "\\ud800"}',
        )
        assert resp.status == 403
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# /ack hardening: oversize, content-type/JSON, wrong token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_oversize_body_rejected(tmp_path: Path) -> None:
    """An oversized ``/ack`` body is rejected (413) and does not delete the file."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        oversize = b"x" * (token_server._MAX_BODY_BYTES + 10)
        resp = await client.post(
            "/ack",
            headers={**_auth(_VALID_NONCE), "Content-Type": "application/json"},
            data=oversize,
        )
        assert resp.status == 413
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_oversize_chunked_body_rejected(tmp_path: Path) -> None:
    """A chunked ``/ack`` body without ``Content-Length`` is capped while reading.

    The declared length is only the cheap first line of defence; a chunked
    request carries no ``Content-Length`` at all, so the cap has to hold on the
    bytes actually received.
    """

    state = _make_state(tmp_path)
    client = await _client(state)

    async def _chunks() -> AsyncIterator[bytes]:
        chunk = b"x" * 65536
        for _ in range(token_server._MAX_BODY_BYTES // 65536 + 1):
            yield chunk

    try:
        resp = await client.post(
            "/ack",
            headers={**_auth(_VALID_NONCE), "Content-Type": "application/json"},
            data=_chunks(),
        )
        assert resp.status == 413
        assert (await resp.json())["error"] == "too_large"
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_non_json_body_rejected(tmp_path: Path) -> None:
    """A non-JSON ``/ack`` body yields 400 and leaves the file intact."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers={**_auth(_VALID_NONCE), "Content-Type": "application/json"},
            data=b"this-is-not-json{",
        )
        assert resp.status == 400
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_wrong_delete_token_forbidden(tmp_path: Path) -> None:
    """A wrong ``delete_token`` yields 403 and never deletes the file."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": "not-the-real-token"},
        )
        assert resp.status == 403
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_wrong_nonce_forbidden_and_counted(tmp_path: Path) -> None:
    """A wrong nonce on ``/ack`` is 403 and feeds the shared lockout counter.

    The counter is server-wide, not per route: otherwise ``/ack`` would be an
    unrate-limited nonce oracle sitting next to the rate-limited ``/secrets``. A
    legitimate client cannot trip this, because it holds the nonce; only the
    ``delete_token`` half of the ack stays outside the counter (retry safety).
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers=_auth("wrong-nonce"),
            json={"delete_token": state.delete_token},
        )
        assert resp.status == 403
        assert state.failed_attempts == 1
        assert state.deleted is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ack_nonce_failures_lock_the_endpoint(tmp_path: Path) -> None:
    """``/ack`` cannot be used to guess the nonce past the lockout threshold."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        statuses = []
        for _ in range(token_server._NONCE_MAX_ATTEMPTS):
            resp = await client.post(
                "/ack",
                headers=_auth("wrong-nonce"),
                json={"delete_token": "guess"},
            )
            statuses.append(resp.status)
        assert statuses[:-1] == [403] * (token_server._NONCE_MAX_ATTEMPTS - 1)
        assert statuses[-1] == 410
        assert state.locked is True
        assert state.shutdown_reason == token_server._SHUTDOWN_LOCKOUT

        # Locked closes the whole endpoint, both routes, even for a correct nonce.
        after_get = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert after_get.status == 410
        after_ack = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": state.delete_token},
        )
        assert after_ack.status == 410
        assert (await after_ack.json())["error"] == "locked"
        # A lockout must never destroy the credential (see the ``_run`` test).
        assert state.deleted is False
        assert state.secrets_path.is_file()
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# ACK idempotency + nonce-burn resistance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_idempotent_second_ack_returns_410(tmp_path: Path) -> None:
    """A second correct ack returns 410 (already gone) without raising an error."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        first = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": state.delete_token},
        )
        assert first.status == 200

        second = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": state.delete_token},
        )
        assert second.status == 410
        assert (await second.json())["status"] == "already_deleted"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_ack_does_not_burn_nonce_for_get(tmp_path: Path) -> None:
    """A retried ack must not burn the nonce: a later correct GET still works.

    The retry scenario the invariant protects (network retry, double click) uses
    the *correct* nonce with a stale/wrong ``delete_token``, so that half stays
    outside the lockout counter entirely. A wrong nonce is counted (it can only
    come from someone who does not hold it) but a single one is far below the
    threshold and must not disturb the handoff.
    """

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        # Repeated wrong delete_token: pure retry noise, never counted.
        for _ in range(token_server._NONCE_MAX_ATTEMPTS + 2):
            bad_token = await client.post(
                "/ack",
                headers=_auth(_VALID_NONCE),
                json={"delete_token": "nope"},
            )
            assert bad_token.status == 403
        assert state.failed_attempts == 0

        # One wrong nonce is counted but stays below the lockout threshold.
        bad_nonce = await client.post(
            "/ack",
            headers=_auth("wrong-nonce"),
            json={"delete_token": state.delete_token},
        )
        assert bad_nonce.status == 403
        assert state.failed_attempts == 1
        assert state.locked is False

        # The bundle is still deliverable.
        resp = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert resp.status == 200
        assert (await resp.json())["bundle"] == _BUNDLE
        assert state.deleted is False
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# TTL fallback delete (no ack)
# ---------------------------------------------------------------------------


def test_ttl_fallback_deletes_secret_without_ack(tmp_path: Path) -> None:
    """Without an ack, the TTL-fallback delete removes the on-disk secret.

    ``_run`` performs this delete after ``asyncio.wait_for`` times out; here we
    drive the same ``_delete_secret_file`` helper the TTL path invokes so the
    fallback is proven without waiting ``_TOKEN_TTL`` seconds.
    """

    state = _make_state(tmp_path)
    assert state.secrets_path.is_file()

    token_server._delete_secret_file(state)

    assert not state.secrets_path.exists()
    assert state.deleted is True


def test_delete_secret_file_is_idempotent(tmp_path: Path) -> None:
    """Deleting twice (ack + TTL fallback) never raises."""

    state = _make_state(tmp_path)
    token_server._delete_secret_file(state)
    # Second call is a no-op (already deleted); must not raise.
    token_server._delete_secret_file(state)
    assert state.deleted is True


def test_delete_secret_file_tolerates_missing_file(tmp_path: Path) -> None:
    """A missing on-disk secret does not fault the delete helper."""

    state = _make_state(tmp_path)
    state.secrets_path.unlink()  # remove out-of-band
    assert (
        token_server._delete_secret_file(state) is True
    )  # FileNotFoundError swallowed
    assert state.deleted is True


def test_delete_secret_file_reports_failure_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine OSError leaves ``deleted`` False and returns False (retryable).

    Regression guard: a permission/transient error on the mounted ``./data``
    directory must not be reported as a successful deletion, otherwise the file
    lingers on disk while the logs and the ack both claim it was removed.
    """

    state = _make_state(tmp_path)

    def _boom(_path: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(token_server.os, "remove", _boom)

    assert token_server._delete_secret_file(state) is False
    assert state.deleted is False
    assert state.secrets_path.is_file()  # still on disk, cleanup can retry


@pytest.mark.asyncio
async def test_ack_reports_500_and_stays_up_when_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed removal makes ``/ack`` return 500 without shutting down.

    The endpoint must stay alive so the client can retry the ack and the TTL
    fallback gets another attempt, instead of stranding the credential while
    reporting success.
    """

    state = _make_state(tmp_path)

    def _boom(_path: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(token_server.os, "remove", _boom)

    client = await _client(state)
    try:
        first = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        delete_token = (await first.json())["delete_token"]

        ack = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": delete_token},
        )
        assert ack.status == 500
        assert (await ack.json())["error"] == "delete_failed"
        assert state.deleted is False
        assert state.secrets_path.is_file()  # not stranded silently
        assert not state.shutdown_event.is_set()  # endpoint stays up for retry
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Full ``_run`` lifecycle: shutdown reason decides whether the file is deleted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ttl_fallback_deletes_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run`` deletes the secret when the TTL elapses without an ack.

    Drives the real timeout path (with a millisecond TTL) instead of only the
    helper, so the shutdown-reason branching introduced for the lockout case
    cannot silently disable the fallback delete.
    """

    secrets_path = _write_secret(tmp_path)
    captured = _patch_run_socket(monkeypatch, _free_loopback_port())
    monkeypatch.setattr(token_server, "_TOKEN_TTL", 0.05)

    await token_server._run(_VALID_NONCE, secrets_path)

    assert captured[0].shutdown_reason == token_server._SHUTDOWN_TTL
    assert captured[0].deleted is True
    assert not secrets_path.exists()


@pytest.mark.asyncio
async def test_run_reports_failed_ttl_fallback_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing TTL-fallback delete is surfaced, not silently treated as done.

    ``_run`` must not mark the credential as consumed when ``os.remove`` fails
    (permission/transient error on the mounted ``./data``); the file is still
    there and the state must say so.
    """

    secrets_path = _write_secret(tmp_path)
    captured = _patch_run_socket(monkeypatch, _free_loopback_port())
    monkeypatch.setattr(token_server, "_TOKEN_TTL", 0.05)

    def _boom(_path: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(token_server.os, "remove", _boom)

    await token_server._run(_VALID_NONCE, secrets_path)

    assert captured[0].shutdown_reason == token_server._SHUTDOWN_TTL
    assert captured[0].deleted is False
    assert secrets_path.is_file()


@pytest.mark.asyncio
async def test_run_lockout_keeps_secret_file_and_closes_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lockout stops the server but leaves ``secrets.json`` on disk.

    The loopback publish is reachable by every local account on the Docker host,
    so five unauthenticated GETs must not be able to destroy freshly minted
    credentials and force the user through a full Google login incl. 2FA again.
    The file is also the carrier of the file-handoff and copy/paste tracks,
    which do not depend on this endpoint. What the lockout *must* do is close
    the endpoint: ``_run`` returns and the port stops accepting connections.
    """

    secrets_path = _write_secret(tmp_path)
    port = _free_loopback_port()
    captured = _patch_run_socket(monkeypatch, port)

    server = asyncio.create_task(token_server._run(_VALID_NONCE, secrets_path))
    try:
        await _wait_until_listening(port)
        url = f"http://127.0.0.1:{port}/secrets"
        async with aiohttp.ClientSession() as session:
            for _ in range(token_server._NONCE_MAX_ATTEMPTS):
                async with session.get(url, headers=_auth("wrong-nonce")) as resp:
                    assert resp.status in (403, 410)
        # The lockout shuts the server down on its own; no cancellation needed.
        await asyncio.wait_for(server, timeout=10)
    finally:
        # No-op on the expected path (the task is already done); only a failing
        # assertion above would leave it running.
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server

    state = captured[0]
    assert state.locked is True
    assert state.shutdown_reason == token_server._SHUTDOWN_LOCKOUT
    # The credential survived the lockout.
    assert state.deleted is False
    assert secrets_path.is_file()
    assert json.loads(secrets_path.read_text(encoding="utf-8")) == _BUNDLE
    # ... but nothing is served any more: the listener is gone.
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)

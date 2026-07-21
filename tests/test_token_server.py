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
* Nonce lockout: a wrong bearer nonce yields ``403`` and, after
  ``_NONCE_MAX_ATTEMPTS`` failures, flips to ``410`` + shutdown.
* Body hardening: an oversized ``/ack`` body is rejected with ``413`` and a
  non-JSON body with ``400``; neither burns the GET lockout counter.
* TTL fallback: the on-disk secret is deleted even when no ack arrives (the
  ``_run`` timeout path), via a direct drive of ``_delete_secret_file``.
* ACK idempotency: a second correct ack returns ``410`` without error, and a
  *failed* ack (wrong ``delete_token``) does not burn the GET nonce.
* Host-publish loopback boundary: the container-internal bind is ``0.0.0.0`` on
  purpose (Docker bridge DNATs the published port onto eth0, not container
  loopback), and the *no-LAN* guarantee is asserted against the HOST publish in
  ``docker-compose.yml`` (``127.0.0.1:7901:7901`` present, no ``0.0.0.0:7901``
  publish), NOT against the container bind.

Collection hardening (Audit HOCH-3): ``docker-login/`` is NOT a Python package
and its basename is not importable via ``import``. The module is loaded with
``importlib.util.spec_from_file_location`` under a synthetic, dotted-free module
name; no ``sys.path`` entry is appended (that would provoke pytest's
``import file mismatch`` because the ``docker-login`` basename collides across
the tree). ``pytest --collect-only tests/test_token_server.py`` must stay green.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
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


# ---------------------------------------------------------------------------
# Collection hardening / bind contract
# ---------------------------------------------------------------------------


def test_module_loads_and_mirrors_constants() -> None:
    """The module loads by path and mirrors the integration const values."""

    # Container-internal bind is 0.0.0.0 on purpose (bridge DNAT); the loopback
    # boundary is the host publish, asserted in the compose test.
    assert token_server._BIND_HOST == "0.0.0.0"
    assert token_server._TOKEN_PORT == 7901
    assert token_server._NONCE_MAX_ATTEMPTS == 5
    assert token_server._TOKEN_TTL == 300


def test_container_bind_is_wildcard_for_bridge_dnat() -> None:
    """The in-container bind is ``0.0.0.0`` so the bridge-DNAT'd publish is reachable.

    This is deliberate: under Docker's default bridge network the published host
    port lands on the container's eth0, not its loopback, so a container-loopback
    bind would make 127.0.0.1:7901 unreachable. The no-LAN boundary is asserted
    separately against the HOST publish (see below), NOT against this bind.
    """

    assert token_server._BIND_HOST == "0.0.0.0"


def test_host_publish_is_loopback_only_no_lan_exposure() -> None:
    """The security boundary: docker-compose publishes 7901 on host loopback only.

    The no-LAN guarantee lives in the host-side publish, not in the container
    bind. Require the loopback publish ``127.0.0.1:7901:7901`` and forbid any
    wildcard publish of the token port (``0.0.0.0:7901`` / a bare ``7901:7901``).
    """

    compose = _COMPOSE_PATH.read_text(encoding="utf-8")
    # The loopback host publish for the token port must be present verbatim.
    assert "127.0.0.1:7901:7901" in compose
    # No wildcard/all-interfaces publish of the token port.
    assert "0.0.0.0:7901" not in compose
    # No bare (all-interfaces) publish of the token port either. A bare
    # "7901:7901" (optionally quoted) with no host-IP prefix binds 0.0.0.0.
    import re

    bare_publish = re.compile(r'(^|[\s"\'])7901:7901(\b)')
    assert bare_publish.search(compose) is None


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

        # Even a subsequently *correct* nonce is refused once locked.
        after = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert after.status == 410
        # The secret was never handed out.
        assert state.delivered is False
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
async def test_ack_wrong_nonce_forbidden(tmp_path: Path) -> None:
    """A wrong nonce on ``/ack`` is 403 and does not touch the GET lockout counter."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        resp = await client.post(
            "/ack",
            headers=_auth("wrong-nonce"),
            json={"delete_token": state.delete_token},
        )
        assert resp.status == 403
        # ACK never increments the GET lockout counter (docstring invariant).
        assert state.failed_attempts == 0
        assert state.deleted is False
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
    """A failed ack must not burn the GET nonce: a later correct GET still works."""

    state = _make_state(tmp_path)
    client = await _client(state)
    try:
        # A failed ack (wrong nonce) followed by a failed ack (wrong token).
        bad_nonce = await client.post(
            "/ack",
            headers=_auth("wrong-nonce"),
            json={"delete_token": state.delete_token},
        )
        assert bad_nonce.status == 403
        bad_token = await client.post(
            "/ack",
            headers=_auth(_VALID_NONCE),
            json={"delete_token": "nope"},
        )
        assert bad_token.status == 403

        # The GET nonce is untouched: the bundle is still deliverable.
        resp = await client.get("/secrets", headers=_auth(_VALID_NONCE))
        assert resp.status == 200
        assert (await resp.json())["bundle"] == _BUNDLE
        assert state.failed_attempts == 0
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

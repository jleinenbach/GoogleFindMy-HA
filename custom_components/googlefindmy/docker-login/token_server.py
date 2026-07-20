#!/usr/bin/env python3
"""One-shot loopback token endpoint for the container login (Track B).

This runs *inside the login container*, after ``main.py`` produced a fresh
``secrets.json``. It hands that bundle to the Home Assistant integration over a
short-lived, nonce-authenticated, loopback-only HTTP endpoint, then deletes the
on-disk secret and shuts itself down.

It is deliberately standalone: it imports **nothing** from the integration
package (``custom_components.googlefindmy.*``). The container runs it without a
Home Assistant install and without the package's ``const.py`` on the path, so
the handful of constants it needs are mirrored locally (kept in sync with
``const.py`` by convention, not by import).

Security model:

* **Container-internal wildcard bind, host-loopback publish.** The server binds
  to ``0.0.0.0`` *inside the login container*. This is deliberate and is NOT the
  security boundary: under Docker's default bridge network, a published port
  (``docker-compose.yml`` publishes ``127.0.0.1:7901:7901``) is DNAT'd onto the
  container's ``eth0`` interface, not onto the container's loopback. A bind to
  the container's ``127.0.0.1`` would therefore make the published port
  unreachable and break the whole handoff. The "no LAN exposure" guarantee is
  enforced by the *host-side publish* being pinned to ``127.0.0.1`` (loopback
  only), not by the in-container bind. The split-machine case is served by an
  SSH tunnel to host loopback, never by exposing a cleartext token endpoint to
  the network (OWASP A02). See ``docker-login/README.md``: ``network_mode: host``
  is unsupported precisely because it would collapse this two-layer boundary and
  make ``0.0.0.0`` LAN-visible.
* **Strong single-use nonce.** The pairing nonce is generated at runtime with
  ``secrets.token_urlsafe(16)`` (>=128 bit) and compared with
  ``hmac.compare_digest`` (constant time). The GET side is single-use with a
  lockout after :data:`_NONCE_MAX_ATTEMPTS` failed attempts (410 + shutdown) and
  a hard TTL (:data:`_TOKEN_TTL`).
* **Two-phase delete.** ``GET /secrets`` returns the bundle plus a random
  ``delete_token`` but does NOT delete the file, so a Home-Assistant-side
  failure does not lose the credential. ``POST /ack`` (nonce + matching
  ``delete_token``) deletes the file and shuts down. If no ack arrives within
  the TTL, the file is deleted anyway (fallback) and the server stops.
* **ACK does not share the GET lockout counter.** A failed/duplicated ack (a
  network retry, a double click) must not burn the nonce or force the user to
  restart the container. A correct ack is idempotent: the second correct ack
  returns 410 (already gone), not an error.
* **No token logging.** The bundle and the nonce/delete_token are never logged;
  only counts/shapes (e.g. ``chars=%d``) are emitted.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger("gfmy.token_server")

# --- Constants mirrored from the integration's const.py (see module docstring).
# The container has no access to the package const.py, so these are kept in sync
# by convention. Keep the values identical.
# Bind to ALL container interfaces. Under Docker's bridge network a published
# port is DNAT'd onto the container's eth0, not its loopback, so a 127.0.0.1
# bind here would make the published 127.0.0.1:7901 host port unreachable. The
# loopback (no-LAN) boundary lives in docker-compose.yml as the HOST publish
# `127.0.0.1:7901:7901`, NOT here. `network_mode: host` is unsupported (README).
_BIND_HOST = "0.0.0.0"  # noqa: S104 - see comment: boundary is the host publish.
_TOKEN_PORT = 7901  # == const.CONTAINER_TOKEN_PORT
_NONCE_BYTES = 16  # secrets.token_urlsafe(16) -> 128 bit; == CONTAINER_NONCE_MIN_LEN
_NONCE_MAX_ATTEMPTS = 5  # == const.CONTAINER_NONCE_MAX_ATTEMPTS
_TOKEN_TTL = 300  # seconds; == const.CONTAINER_TOKEN_TTL
_MAX_BODY_BYTES = 1024 * 1024  # == const.CONTAINER_MAX_RESPONSE_BYTES


class _State:
    """Mutable server state shared across handlers (single event loop, no lock)."""

    def __init__(self, nonce: str, secrets_path: Path) -> None:
        self.nonce = nonce
        self.secrets_path = secrets_path
        # A second random token, returned by GET and required by POST /ack. It
        # ties the delete to the party that actually received the bundle.
        self.delete_token = secrets.token_urlsafe(_NONCE_BYTES)
        self.failed_attempts = 0
        self.delivered = False  # a GET succeeded at least once
        self.deleted = False  # the file has been removed (ack or TTL)
        self.locked = False  # too many failed GET attempts
        self.shutdown_event = asyncio.Event()


def _check_nonce(state: _State, request: web.Request) -> bool:
    """Constant-time bearer-nonce check. Does NOT touch the lockout counter."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    presented = header[len(prefix) :] if header.startswith(prefix) else ""
    return bool(presented) and hmac.compare_digest(presented, state.nonce)


def _delete_secret_file(state: _State) -> None:
    """Best-effort delete of the on-disk secret; idempotent."""
    if state.deleted:
        return
    try:
        os.remove(state.secrets_path)
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.warning("Failed to remove secrets file: %s", err)
    state.deleted = True


async def _handle_secrets(request: web.Request) -> web.Response:
    """``GET /secrets``: nonce-gated, single-use, returns bundle + delete_token."""
    state: _State = request.app["state"]

    if state.locked:
        return web.json_response({"error": "locked"}, status=410)

    if not _check_nonce(state, request):
        state.failed_attempts += 1
        _LOGGER.warning(
            "Rejected /secrets (bad pairing code, attempt %d/%d)",
            state.failed_attempts,
            _NONCE_MAX_ATTEMPTS,
        )
        if state.failed_attempts >= _NONCE_MAX_ATTEMPTS:
            state.locked = True
            _LOGGER.error("Pairing lockout reached; shutting the endpoint down.")
            state.shutdown_event.set()
            return web.json_response({"error": "locked"}, status=410)
        return web.json_response({"error": "forbidden"}, status=403)

    if state.deleted:
        return web.json_response({"error": "gone"}, status=410)

    try:
        with open(state.secrets_path, encoding="utf-8") as fh:
            bundle = json.load(fh)
    except FileNotFoundError:
        return web.json_response({"error": "gone"}, status=410)
    except (OSError, ValueError) as err:
        _LOGGER.error("Failed to read secrets file: %s", err)
        return web.json_response({"error": "read_failed"}, status=500)

    state.delivered = True
    _LOGGER.info("Delivered secrets bundle (keys=%d) over loopback.", len(bundle))
    # NOTE: file is NOT deleted here (two-phase delete); POST /ack or TTL does it.
    return web.json_response({"bundle": bundle, "delete_token": state.delete_token})


async def _handle_ack(request: web.Request) -> web.Response:
    """``POST /ack``: nonce + delete_token -> delete file + shutdown. Idempotent.

    Deliberately does NOT increment the GET lockout counter: a failed/retried ack
    must not burn the nonce (Audit MITTEL-1).
    """
    state: _State = request.app["state"]

    if not _check_nonce(state, request):
        return web.json_response({"error": "forbidden"}, status=403)

    if request.content_length and request.content_length > _MAX_BODY_BYTES:
        return web.json_response({"error": "too_large"}, status=413)
    try:
        raw = await request.content.read(_MAX_BODY_BYTES + 1)
        if len(raw) > _MAX_BODY_BYTES:
            return web.json_response({"error": "too_large"}, status=413)
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    presented = body.get("delete_token", "") if isinstance(body, dict) else ""
    if not isinstance(presented, str) or not hmac.compare_digest(
        presented, state.delete_token
    ):
        return web.json_response({"error": "forbidden"}, status=403)

    if state.deleted:
        # Idempotent: a duplicate correct ack is a success, not an error.
        state.shutdown_event.set()
        return web.json_response({"status": "already_deleted"}, status=410)

    _delete_secret_file(state)
    _LOGGER.info("Consumption acked; secret deleted, endpoint shutting down.")
    state.shutdown_event.set()
    return web.json_response({"status": "deleted"}, status=200)


def _build_app(state: _State) -> web.Application:
    app = web.Application(client_max_size=_MAX_BODY_BYTES)
    app["state"] = state
    app.router.add_get("/secrets", _handle_secrets)
    app.router.add_post("/ack", _handle_ack)
    return app


async def _run(nonce: str, secrets_path: Path) -> None:
    """Run the endpoint until ack, lockout, or TTL, then delete + stop."""
    state = _State(nonce, secrets_path)
    app = _build_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    # Bind all container interfaces (0.0.0.0): the Docker bridge DNATs the
    # published host port onto eth0, so a container-loopback bind would be
    # unreachable. The no-LAN boundary is the host publish (docker-compose.yml).
    site = web.TCPSite(runner, _BIND_HOST, _TOKEN_PORT)
    await site.start()
    _LOGGER.info(
        "Token endpoint listening on %s:%d (TTL %ds).",
        _BIND_HOST,
        _TOKEN_PORT,
        _TOKEN_TTL,
    )
    try:
        try:
            await asyncio.wait_for(state.shutdown_event.wait(), timeout=_TOKEN_TTL)
        except asyncio.TimeoutError:
            _LOGGER.warning("TTL elapsed without ack; deleting secret (fallback).")
        # TTL-fallback delete: ensure no secret lingers even without an ack.
        _delete_secret_file(state)
    finally:
        await runner.cleanup()


def _resolve_secrets_path() -> Path:
    """Resolve the secrets.json path the same way main.py does (env override)."""
    override = os.environ.get("GOOGLEFINDMY_SECRETS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path("/data/secrets.json")


def main() -> int:
    """Entry point invoked by entrypoint.sh under GFMY_ONECLICK=1.

    The pairing nonce is taken from the ``GFMY_PAIRING_CODE`` environment
    variable (generated + printed by entrypoint.sh), never a compose default.
    """
    logging.basicConfig(level=logging.INFO, format="[token_server] %(message)s")
    nonce = os.environ.get("GFMY_PAIRING_CODE", "").strip()
    if len(nonce) < _NONCE_BYTES:
        _LOGGER.error("Missing/short GFMY_PAIRING_CODE; refusing to start.")
        return 2
    secrets_path = _resolve_secrets_path()
    if not secrets_path.is_file():
        _LOGGER.error("No secrets file at the resolved path; nothing to serve.")
        return 3
    try:
        asyncio.run(_run(nonce, secrets_path))
    except KeyboardInterrupt:  # pragma: no cover - signal path
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

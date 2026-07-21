#!/usr/bin/env python3
"""One-shot loopback token endpoint for the container login (Track B).

This runs *inside the login container*, after ``main.py`` produced a fresh
``secrets.json``. It hands that bundle to the Home Assistant integration over a
short-lived, nonce-authenticated, loopback-only HTTP endpoint, then deletes the
on-disk secret and shuts itself down. The delete happens once the handoff is
over (ack or TTL); a lockout only shuts the endpoint down and keeps the file
(see "Security model" below).

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
  ``hmac.compare_digest`` (constant time) over the *encoded bytes*
  (:func:`_constant_time_equals`); ``compare_digest`` raises ``TypeError`` on
  ``str`` arguments holding non-ASCII code points, which HTTP headers (latin-1
  on the wire) and JSON bodies can both carry. A non-ASCII credential is
  therefore an ordinary mismatch (403 + counted), never a 500 that would slip
  past the counter. There is a lockout after :data:`_NONCE_MAX_ATTEMPTS` failed
  attempts (410 + shutdown) and a hard TTL (:data:`_TOKEN_TTL`).
* **The lockout is server-wide, not per route.** Both handlers feed the same
  counter through :func:`_register_nonce_failure` and both refuse everything
  once ``locked`` is set, so the guessing budget cannot be doubled by switching
  endpoints. Without that, ``POST /ack`` would be an unrate-limited nonce
  oracle (403 for a wrong nonce vs. 413 for a correct one with an oversized
  body). That is only theoretical at 128 bit of entropy within a 300 s TTL, but
  the nonce is supplied at runtime via ``GFMY_PAIRING_CODE`` and its strength is
  not this module's to guarantee, so the bound is enforced here.
* **A lockout closes the endpoint; it does not destroy the credential.**
  Reaching the lockout stops the server (Track B is over) but leaves
  ``secrets.json`` on disk, because anyone with a local account on the Docker
  host can reach the loopback publish and five unauthenticated GETs must not be
  able to force a full Google re-login. The file is also the carrier of Track A
  (file handoff) and Track C (copy/paste), which are independent of the network
  path. Deletion happens only for the *consumed* shutdown reasons, ack and TTL
  (see :func:`_run` and :data:`_SHUTDOWN_LOCKOUT`).
* **One-shot delivery.** ``GET /secrets`` succeeds exactly once: the first
  success sets ``delivered`` and every further GET is refused with ``410``,
  independently of whether the file is still on disk. The two-phase delete below
  keeps the file until the ack, so without this check the nonce holder could
  re-collect the credentials until the ack or the TTL fired. ``POST /ack``
  deliberately stays reachable after a delivery (though not after a lockout, see
  above) so the delete can still be completed.
* **Two-phase delete.** ``GET /secrets`` returns the bundle plus a random
  ``delete_token`` but does NOT delete the file, so a Home-Assistant-side
  failure does not lose the credential. ``POST /ack`` (nonce + matching
  ``delete_token``) deletes the file and shuts down. If no ack arrives within
  the TTL, the file is deleted anyway (fallback) and the server stops.
* **A wrong delete_token never burns the nonce.** A failed/duplicated ack (a
  network retry, a double click) must not force the user to restart the
  container, so ``delete_token`` mismatches stay outside the lockout counter.
  Nonce mismatches are different: a legitimate client cannot produce one,
  because it holds the nonce. A correct ack is idempotent: the second correct
  ack returns 410 (already gone), not an error.
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
# The bandit suppression below therefore stays. Keep its marker bare (id only,
# no trailing prose, and do not repeat the marker itself in prose): bandit parses
# every word after it as a further test id and warns "Test in comment: ... is not
# a test name or id, ignoring" for each one.
_BIND_HOST = "0.0.0.0"  # noqa: S104  # nosec B104
_TOKEN_PORT = 7901  # == const.CONTAINER_TOKEN_PORT
_NONCE_BYTES = 16  # secrets.token_urlsafe(16) -> 128 bit; == CONTAINER_NONCE_MIN_LEN
_NONCE_MAX_ATTEMPTS = 5  # == const.CONTAINER_NONCE_MAX_ATTEMPTS
_TOKEN_TTL = 300  # seconds; == const.CONTAINER_TOKEN_TTL
_MAX_BODY_BYTES = 1024 * 1024  # == const.CONTAINER_MAX_RESPONSE_BYTES

# Why the server stopped. Only the "consumed" reasons delete the on-disk secret;
# a lockout closes the endpoint and deliberately keeps the file (module
# docstring, Security model).
_SHUTDOWN_ACK = "ack"
_SHUTDOWN_LOCKOUT = "lockout"
_SHUTDOWN_TTL = "ttl"


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
        self.locked = False  # too many failed nonce attempts (any endpoint)
        self.shutdown_event = asyncio.Event()
        self.shutdown_reason: str | None = None  # one of the _SHUTDOWN_* values

    def request_shutdown(self, reason: str) -> None:
        """Wake :func:`_run` and record *why* the server stops.

        The first reason wins: it decides whether ``_run`` deletes the secret,
        so a later event must not relabel a lockout as a consumed handoff.
        """
        if self.shutdown_reason is None:
            self.shutdown_reason = reason
        self.shutdown_event.set()


def _constant_time_equals(presented: str, expected: str) -> bool:
    """Compare a presented secret with the expected one in constant time.

    ``hmac.compare_digest`` raises ``TypeError`` as soon as a ``str`` argument
    contains a non-ASCII code point, and both credential carriers here can hold
    one: HTTP header values are latin-1 on the wire (``Bearer b\\xe4d`` is a
    legal request) and a JSON body may carry arbitrary Unicode, including a lone
    surrogate via a ``\\ud800`` escape. Unhandled, that turns a wrong credential
    into a 500 that never reaches the lockout counter.

    Comparing the *encoded bytes* keeps one constant-time primitive over the
    whole value. An ``isascii()`` pre-rejection was deliberately not used: it
    bails out on the first offending code point and so puts an input-dependent
    early exit in front of the constant-time path.

    ``utf-8``/``surrogatepass`` is the encoding pair that never raises: plain
    ``utf-8`` rejects lone surrogates and ``surrogateescape`` only round-trips
    U+DC80..U+DCFF. The mapping is injective, so it cannot make two different
    secrets compare equal.
    """
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def _check_nonce(state: _State, request: web.Request) -> bool:
    """Constant-time bearer-nonce check. Does NOT touch the lockout counter."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    presented = header[len(prefix) :] if header.startswith(prefix) else ""
    return bool(presented) and _constant_time_equals(presented, state.nonce)


def _locked_response() -> web.Response:
    """Uniform refusal once the endpoint is locked (both routes)."""
    return web.json_response({"error": "locked"}, status=410)


def _register_nonce_failure(state: _State, route: str) -> web.Response:
    """Count one failed nonce presentation and build the rejection response.

    Shared by both handlers so the guessing budget is server-wide: the lockout
    cannot be doubled (or bypassed entirely) by switching to the other route.
    Reaching :data:`_NONCE_MAX_ATTEMPTS` locks the endpoint and shuts it down,
    but does NOT delete the secret (see :func:`_run`).
    """
    state.failed_attempts += 1
    _LOGGER.warning(
        "Rejected %s (bad pairing code, attempt %d/%d)",
        route,
        state.failed_attempts,
        _NONCE_MAX_ATTEMPTS,
    )
    if state.failed_attempts >= _NONCE_MAX_ATTEMPTS:
        state.locked = True
        _LOGGER.error("Pairing lockout reached; shutting the endpoint down.")
        state.request_shutdown(_SHUTDOWN_LOCKOUT)
        return _locked_response()
    return web.json_response({"error": "forbidden"}, status=403)


def _delete_secret_file(state: _State) -> bool:
    """Delete the on-disk secret; idempotent.

    Returns ``True`` when the file is gone (removed now or already absent) and
    ``False`` when removal genuinely failed (e.g. a transient/permission
    ``OSError`` on the mounted ``./data`` directory). On failure ``deleted``
    stays ``False`` so the caller can surface an error and the TTL fallback (or
    a client retry) re-attempts the removal instead of leaving a credential on
    disk while the logs claim success.
    """
    if state.deleted:
        return True
    try:
        os.remove(state.secrets_path)
    except FileNotFoundError:
        pass  # Already gone: treat as deleted.
    except OSError as err:
        _LOGGER.warning("Failed to remove secrets file: %s", err)
        return False
    state.deleted = True
    return True


async def _handle_secrets(request: web.Request) -> web.Response:
    """``GET /secrets``: nonce-gated, one-shot, returns bundle + delete_token.

    One-shot is enforced on the *delivery*, not only on the file: once a GET has
    handed the bundle out, every further GET is refused with ``410``, even while
    the file still exists (the two-phase delete keeps it until the ack). Without
    that check anyone holding the pairing code could re-collect the credentials
    until the ack or the TTL fired. ``POST /ack`` stays reachable so the client
    that received the bundle can still complete the delete (unless a lockout
    closed the endpoint in the meantime).
    """
    state: _State = request.app["state"]

    if state.locked:
        return _locked_response()

    if not _check_nonce(state, request):
        return _register_nonce_failure(state, "/secrets")

    if state.delivered or state.deleted:
        # Both are terminal for GET and share the 410, but stay distinguishable
        # in the payload. ``delivered`` is checked first and independently of the
        # file: the two-phase delete keeps the bundle on disk until the ack, so
        # without this gate the pairing-code holder could re-collect it.
        error = "already_delivered" if state.delivered else "gone"
        _LOGGER.warning("Rejected /secrets: %s.", error)
        return web.json_response({"error": error}, status=410)

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


async def _read_limited_body(request: web.Request) -> bytes | None:
    """Read the request body capped at :data:`_MAX_BODY_BYTES`.

    Returns the raw bytes, or ``None`` when the body exceeds the cap. Both the
    declared ``Content-Length`` (cheap rejection before reading) and the number
    of bytes actually received are checked, because the header may be absent
    (chunked transfer) or simply lie.

    The receive side is drained in a loop: ``StreamReader.read(n)`` returns *up
    to* n bytes, i.e. whatever is buffered, so a single call would both miss an
    oversized chunked body (short read -> looks small) and truncate a legitimate
    multi-chunk body into a bogus ``400``. Reading until the cap or EOF keeps the
    memory bound at ``_MAX_BODY_BYTES + 1`` while making the verdict exact. Only
    a caller that already passed the nonce check gets this far, so the loop is
    not an unauthenticated work multiplier.
    """
    declared = request.content_length
    if declared is not None and declared > _MAX_BODY_BYTES:
        return None
    raw = bytearray()
    while len(raw) <= _MAX_BODY_BYTES:
        chunk = await request.content.read(_MAX_BODY_BYTES + 1 - len(raw))
        if not chunk:
            return bytes(raw)
        raw += chunk
    return None


# The PLR0911 suppression on the next line is a deliberate call, not laziness:
# the handler is a flat chain of guard clauses (locked, nonce, body size, JSON
# shape, delete_token, delete outcome), each answering with its own status code.
# Nesting them or funnelling them through a result object would hide the
# rejection reasons rather than simplify them. Everything that could genuinely be
# factored out already is: _locked_response and _register_nonce_failure (shared
# with /secrets) and _read_limited_body.
async def _handle_ack(request: web.Request) -> web.Response:  # noqa: PLR0911
    """``POST /ack``: nonce + delete_token -> delete file + shutdown. Idempotent.

    A wrong ``delete_token`` deliberately does NOT touch the lockout counter: a
    failed/retried ack must not burn the nonce (Audit MITTEL-1). A wrong *nonce*
    does count, because a legitimate client holds the nonce and can only fail on
    the token; leaving this route uncounted would make it an unrate-limited
    nonce oracle next to the counted ``GET /secrets`` (module docstring).
    """
    state: _State = request.app["state"]

    if state.locked:
        # Once locked the whole endpoint is closed, not just the GET route.
        return _locked_response()

    if not _check_nonce(state, request):
        return _register_nonce_failure(state, "/ack")

    raw = await _read_limited_body(request)
    if raw is None:
        return web.json_response({"error": "too_large"}, status=413)
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "bad_request"}, status=400)

    presented = body.get("delete_token", "") if isinstance(body, dict) else ""
    if not isinstance(presented, str) or not _constant_time_equals(
        presented, state.delete_token
    ):
        return web.json_response({"error": "forbidden"}, status=403)

    if state.deleted:
        # Idempotent: a duplicate correct ack is a success, not an error.
        state.request_shutdown(_SHUTDOWN_ACK)
        return web.json_response({"status": "already_deleted"}, status=410)

    if not _delete_secret_file(state):
        # Removal genuinely failed. Do NOT claim success or shut down: keep the
        # endpoint alive so the client can retry the ack and the TTL fallback
        # gets another attempt, rather than stranding the credential on disk.
        return web.json_response({"error": "delete_failed"}, status=500)
    _LOGGER.info("Consumption acked; secret deleted, endpoint shutting down.")
    state.request_shutdown(_SHUTDOWN_ACK)
    return web.json_response({"status": "deleted"}, status=200)


def _build_app(state: _State) -> web.Application:
    app = web.Application(client_max_size=_MAX_BODY_BYTES)
    app["state"] = state
    app.router.add_get("/secrets", _handle_secrets)
    app.router.add_post("/ack", _handle_ack)
    return app


async def _run(nonce: str, secrets_path: Path) -> None:
    """Run the endpoint until ack, lockout, or TTL, then clean up and stop.

    The on-disk secret is deleted for the *consumed* shutdown reasons only: the
    ack (the bundle reached Home Assistant) and the TTL fallback (nobody is
    coming, do not leave a credential lying around). After a lockout the file is
    kept: the loopback publish is reachable by every local account on the Docker
    host, so five unauthenticated GETs must not be able to destroy the freshly
    minted credentials and force a full Google re-login. The file also carries
    Track A (file handoff) and Track C (copy/paste), which do not depend on this
    endpoint at all.
    """
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
        except TimeoutError:
            state.request_shutdown(_SHUTDOWN_TTL)
            _LOGGER.warning("TTL elapsed without ack; deleting secret (fallback).")
        if state.shutdown_reason == _SHUTDOWN_LOCKOUT:
            # Lockout: close the endpoint, keep the file. Deleting it here would
            # let five unauthenticated loopback GETs wipe the credentials and
            # break the file/copy-paste handoff tracks along with them.
            _LOGGER.warning(
                "Lockout: endpoint closed, secrets file kept at %s "
                "(file handoff and copy/paste stay available).",
                state.secrets_path,
            )
        elif not _delete_secret_file(state):
            # Ack or TTL fallback: ensure no secret lingers.
            _LOGGER.error(
                "Post-handoff delete failed; secret may still linger at %s.",
                state.secrets_path,
            )
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

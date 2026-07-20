"""Loopback container-login client (Track B one-click handoff).

This is the Home-Assistant-side half of the "one-click" Docker login: it pulls a
freshly minted ``secrets.json`` bundle from the login container's short-lived
loopback token endpoint (see ``docker-login/token_server.py``) over plain HTTP.

Design (deep module, narrow API):

* :func:`fetch_secrets_from_container` performs an authenticated ``GET /secrets``
  and returns ``(bundle, delete_token)``. It does **not** validate the bundle --
  the config flow runs it through ``normalize_secrets_bundle`` +
  ``async_pick_working_token`` exactly like the paste path, so there is only one
  validation pipeline and no second divergence source.
* :func:`ack_consumed` performs the second phase of the two-phase delete
  (``POST /ack``): only after Home Assistant has confirmed the bundle works does
  the container delete the on-disk secret and shut the endpoint down. Failing to
  ack simply leaves the container to delete on its TTL fallback.

Security invariants (module-internal, not toggleable by callers):

* ``allow_redirects=False`` -- a redirect from the target must never be followed;
  otherwise a compromised/misconfigured endpoint could bounce the request (with
  its ``Authorization`` header) to an internal metadata service (SSRF, OWASP
  A10). A redirect status is surfaced as :class:`ContainerUnreachableError`.
* ``http``-scheme pinning -- the URL is built here from ``host``/``port`` with a
  fixed ``http://`` scheme; callers cannot smuggle ``file://``/``https://`` etc.
* Response size ceiling (:data:`CONTAINER_MAX_RESPONSE_BYTES`) -- the body is
  read with a hard cap so a hostile endpoint cannot exhaust memory.
* Content-Type check -- only ``application/json`` is parsed.
* Separate ``sock_read`` timeout -- a stalled socket cannot hang the flow past
  the read budget even if the total timeout is generous.

SSRF honesty (do not overclaim): the *primary* protection is that the default
target is loopback (``127.0.0.1``) and redirects are refused. The user types the
host themselves (a deliberate target -- the split-machine case is an SSH tunnel
to ``127.0.0.1``), so private/loopback IPs are intentionally **not** rejected
wholesale; a blanket RFC1918 ban would break the ``127.0.0.1`` main case. The
``169.254.0.0/16`` link-local block below is *best-effort only*: it catches the
IPv4 cloud-metadata address (``169.254.169.254``) when the user typed a literal
IPv4, but it does **not** cover IPv6 metadata (``fd00:ec2::254``, ``fe80::``),
DNS names that resolve to a metadata address (``metadata.google.internal``,
DNS-rebinding of a typed hostname), or a proxy in front of the endpoint. Full
coverage would require resolving the host to an IP before connect and matching
it against a link-local/metadata deny-list; that is deliberately out of scope
here because the loopback default plus no-redirect already carries the weight.

No token logging: bundle and token contents are never logged; only shapes/counts
(e.g. ``chars=%d``) are ever emitted.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Final

import aiohttp

from .const import CONTAINER_MAX_RESPONSE_BYTES

_LOGGER = logging.getLogger(__name__)

_JSON_CONTENT_TYPE: Final = "application/json"
# Any 3xx status is treated as a refused redirect / unexpected reply.
_HTTP_REDIRECT_FLOOR: Final = 300
# Best-effort link-local / cloud-metadata block (IPv4 literals only; see the
# module docstring for what this intentionally does NOT cover).
_LINK_LOCAL_V4: Final = ipaddress.ip_network("169.254.0.0/16")


class ContainerLoginError(Exception):
    """Base class for all container-login client failures."""


class ContainerAuthError(ContainerLoginError):
    """The pairing nonce was rejected (wrong code, expired, or locked out)."""


class ContainerUnreachableError(ContainerLoginError):
    """The endpoint could not be reached, or answered unexpectedly.

    Covers connection refused/reset, a refused redirect, a wrong content type,
    an oversized body, and any non-auth HTTP error status.
    """


class ContainerTimeoutError(ContainerLoginError):
    """The request exceeded its timeout budget."""


def _maybe_block_link_local(host: str) -> None:
    """Reject a literal IPv4 link-local/metadata host (best-effort only)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP (a hostname / IPv6-with-brackets). We do NOT resolve
        # here; see the module docstring for the honest scope of this guard.
        return
    if isinstance(ip, ipaddress.IPv4Address) and ip in _LINK_LOCAL_V4:
        raise ContainerUnreachableError(
            "refusing to contact an IPv4 link-local/metadata address"
        )


def _build_base_url(host: str, port: int) -> str:
    """Build the pinned ``http://host:port`` base URL.

    Scheme is fixed to ``http`` here; a bracketed IPv6 literal is passed through
    unchanged (aiohttp/yarl handle the brackets).
    """
    return f"http://{host}:{port}"


def _make_timeout(timeout: float) -> aiohttp.ClientTimeout:
    """Total budget plus a separate ``sock_read`` so a stalled read cannot hang."""
    return aiohttp.ClientTimeout(total=timeout, sock_read=timeout)


async def _read_json_capped(response: aiohttp.ClientResponse) -> Any:
    """Read + parse a JSON body under a hard size cap and content-type check."""
    ctype = response.content_type
    if ctype != _JSON_CONTENT_TYPE:
        raise ContainerUnreachableError(
            f"unexpected content-type from container endpoint: {ctype!r}"
        )
    raw = await response.content.read(CONTAINER_MAX_RESPONSE_BYTES + 1)
    if len(raw) > CONTAINER_MAX_RESPONSE_BYTES:
        raise ContainerUnreachableError(
            f"container response exceeded {CONTAINER_MAX_RESPONSE_BYTES} bytes"
        )
    import json  # noqa: PLC0415

    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise ContainerUnreachableError(
            "container response was not valid JSON"
        ) from err


async def fetch_secrets_from_container(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    nonce: str,
    *,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """Fetch the freshly minted secrets bundle from the login container.

    Performs an authenticated ``GET {base}/secrets`` with the pairing nonce as a
    bearer token and returns ``(bundle, delete_token)``. The bundle is returned
    verbatim: validation is the caller's job (``normalize_secrets_bundle`` +
    ``async_pick_working_token``), so this module never becomes a second
    validation path.

    Raises:
        ContainerAuthError: the nonce was rejected (HTTP 401/403/410).
        ContainerTimeoutError: the request timed out.
        ContainerUnreachableError: connection/redirect/content/size failure, or
            a malformed response missing the expected fields.
    """
    _maybe_block_link_local(host)
    url = f"{_build_base_url(host, port)}/secrets"
    headers = {"Authorization": f"Bearer {nonce}"}
    _LOGGER.debug(
        "Fetching container secrets from %s:%d (nonce chars=%d)", host, port, len(nonce)
    )
    try:
        async with session.get(
            url,
            headers=headers,
            allow_redirects=False,
            timeout=_make_timeout(timeout),
        ) as response:
            _raise_for_auth(response.status)
            if response.status >= _HTTP_REDIRECT_FLOOR:
                raise ContainerUnreachableError(
                    f"container endpoint returned HTTP {response.status}"
                )
            payload = await _read_json_capped(response)
    except (aiohttp.ServerTimeoutError, TimeoutError) as err:
        # aiohttp raises the builtin TimeoutError on a total timeout; it is not a
        # ServerTimeoutError nor a ClientError subclass, so catch it explicitly.
        raise ContainerTimeoutError(str(err)) from err
    except aiohttp.ClientError as err:
        raise ContainerUnreachableError(str(err)) from err

    if not isinstance(payload, dict):
        raise ContainerUnreachableError("container response was not a JSON object")
    bundle = payload.get("bundle")
    delete_token = payload.get("delete_token")
    if (
        not isinstance(bundle, dict)
        or not isinstance(delete_token, str)
        or not delete_token
    ):
        raise ContainerUnreachableError(
            "container response missing 'bundle'/'delete_token'"
        )
    _LOGGER.debug("Received container bundle (keys=%d)", len(bundle))
    return bundle, delete_token


async def ack_consumed(  # noqa: PLR0913 - fixed two-phase-delete API contract
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    nonce: str,
    delete_token: str,
    *,
    timeout: float,
) -> None:
    """Confirm consumption so the container deletes the secret and shuts down.

    Second phase of the two-phase delete: called only after Home Assistant has
    accepted the bundle. A failure here is non-fatal for the caller -- the
    container deletes on its own TTL fallback -- but is surfaced as a typed
    error so the caller can log it (never the token itself).
    """
    _maybe_block_link_local(host)
    url = f"{_build_base_url(host, port)}/ack"
    headers = {"Authorization": f"Bearer {nonce}"}
    body = {"delete_token": delete_token}
    _LOGGER.debug("Acking container consumption at %s:%d", host, port)
    try:
        async with session.post(
            url,
            headers=headers,
            json=body,
            allow_redirects=False,
            timeout=_make_timeout(timeout),
        ) as response:
            _raise_for_auth(response.status)
            # 200 (deleted now) and 410 (already gone / TTL fired) are both an
            # idempotent success: the on-disk secret is confirmed absent.
            if response.status not in (200, 410):
                raise ContainerUnreachableError(
                    f"container ack returned HTTP {response.status}"
                )
    except (aiohttp.ServerTimeoutError, TimeoutError) as err:
        # aiohttp raises the builtin TimeoutError on a total timeout; it is not a
        # ServerTimeoutError nor a ClientError subclass, so catch it explicitly.
        raise ContainerTimeoutError(str(err)) from err
    except aiohttp.ClientError as err:
        raise ContainerUnreachableError(str(err)) from err


def _raise_for_auth(status: int) -> None:
    """Map an auth-class status to :class:`ContainerAuthError`."""
    if status in (401, 403):
        raise ContainerAuthError(
            f"container endpoint rejected the pairing code (HTTP {status})"
        )

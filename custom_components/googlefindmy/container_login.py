# custom_components/googlefindmy/container_login.py
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
  ack usually just leaves the container to delete on its TTL fallback -- except
  after a lockout, where it keeps the file on purpose. Success is therefore
  claimed only when the response body confirms the deletion; the status is not
  enough, because the lockout refusal shares the ``410`` of the idempotent
  success (see :func:`_require_ack_deleted`).

Security invariants (module-internal, not toggleable by callers):

* ``allow_redirects=False`` -- a redirect from the target must never be followed;
  otherwise a compromised/misconfigured endpoint could bounce the request (with
  its ``Authorization`` header) to an internal metadata service (SSRF, OWASP
  A10). A redirect status is surfaced as :class:`ContainerUnreachableError`.
* ``http``-scheme pinning -- the URL is built here from ``host``/``port`` with a
  fixed ``http://`` scheme; callers cannot smuggle ``file://``/``https://`` etc.
* Response size ceiling (:data:`CONTAINER_MAX_RESPONSE_BYTES`) -- the body is
  drained to EOF in a loop (a chunked reply must not be truncated) but never
  buffers more than the cap plus the single byte that proves it was exceeded, so
  a hostile endpoint cannot exhaust memory.
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
# 410 Gone: asymmetric between the two paths, and on the ack path additionally
# payload-conditional (see _raise_for_auth and _require_ack_deleted).
_HTTP_GONE: Final = 410
_HTTP_OK: Final = 200
# ACK-path body markers (token_server.py): the server states the outcome in the
# payload, not in the status. ``deleted`` accompanies the 200, ``already_deleted``
# the idempotent 410, ``locked`` the lockout refusal that shares that same 410.
_ACK_DELETED_MARKERS: Final = frozenset({"deleted", "already_deleted"})
_LOCKED_MARKER: Final = "locked"
# Best-effort link-local / cloud-metadata block (IPv4 literals only; see the
# module docstring for what this intentionally does NOT cover).
_LINK_LOCAL_V4: Final = ipaddress.ip_network("169.254.0.0/16")


class ContainerLoginError(Exception):
    """Base class for all container-login client failures.

    Attributes:
        secret_retained: ``True`` only where the endpoint *proved* that it is
            keeping its ``secrets.json``, which today is exactly the ack-path
            lockout refusal (:func:`_require_ack_deleted`). The flag states the
            **fact** a caller has to act on -- the credential survives on disk
            and no TTL delete follows -- rather than the cause, so it lives on
            the base class: the error *class* is not a reliable proxy for that
            fact (a plain ``403`` is also a :class:`ContainerAuthError` but
            leaves the TTL delete intact), and a future path that establishes
            the same fact through a different class can set it without a
            hierarchy change. ``False`` everywhere else, which reads "not known
            to be retained" -- never "known to be deleted". Unreachable
            endpoints, timeouts and unclassifiable bodies leave the container's
            state genuinely unknown and therefore keep the default.
    """

    def __init__(self, message: str, *, secret_retained: bool = False) -> None:
        """Store the message plus the "the container kept it" discriminator.

        ``secret_retained`` is keyword-only and defaults to ``False`` so every
        existing single-argument construction keeps working unchanged.
        """
        super().__init__(message)
        self.secret_retained = secret_retained


class ContainerAuthError(ContainerLoginError):
    """The pairing nonce was rejected (wrong code, expired, or locked out).

    Attributes:
        code_used: ``True`` only for the *fetch*-path ``HTTP 410``, i.e. the
            endpoint has nothing left to hand out: the pairing code expired,
            was locked out, or the one-shot bundle was already collected. The
            code the user typed may well have been correct, so the caller can
            show a "start the login container again for a fresh code" hint
            instead of the misleading "check the pairing code". ``False`` for a
            plain rejection (``401``/``403``), where re-typing the code is the
            right advice, ``False`` for the *ack*-path lockout ``410`` (the flow
            is long past the code entry there; that error only ever reaches a
            log, and it identifies itself through
            :attr:`ContainerLoginError.secret_retained` instead), and ``False``
            for every construction that does not pass the flag (backwards
            compatible).
    """

    def __init__(
        self,
        message: str,
        *,
        code_used: bool = False,
        secret_retained: bool = False,
    ) -> None:
        """Store the message plus the fetch-path ``410`` discriminator.

        ``code_used`` is keyword-only and defaults to ``False`` so existing
        single-argument constructions keep working unchanged; ``secret_retained``
        is forwarded to the base class (see :class:`ContainerLoginError`). The
        two flags are independent: they answer "was the pairing code spent?" and
        "does the container still hold the credential?", and no path sets both.
        """
        super().__init__(message, secret_retained=secret_retained)
        self.code_used = code_used


class ContainerUnreachableError(ContainerLoginError):
    """The endpoint could not be reached, or answered unexpectedly.

    Covers connection refused/reset, a refused redirect, a wrong content type,
    an oversized body, any non-auth HTTP error status, and an ack-path ``410``
    whose body does not confirm that the secret was deleted.
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


async def _read_body_capped(response: aiohttp.ClientResponse) -> bytes:
    """Drain the response body to EOF, refusing anything above the size ceiling.

    ``StreamReader.read(n)`` returns *up to* n bytes, i.e. whatever happens to be
    buffered right now, so a single call truncates a body that arrives in several
    TCP chunks and hands partial JSON to the parser -- a valid handoff would then
    fail intermittently. The loop reads until EOF while keeping the memory bound
    at ``CONTAINER_MAX_RESPONSE_BYTES + 1`` bytes: one byte past the ceiling is
    all that is needed to prove the body is oversized, and nothing is ever read
    unbounded. Same shape as the server side
    (``docker-login/token_server.py::_read_limited_body``), so both ends of the
    handoff bound their reads identically.

    Raises:
        ContainerUnreachableError: the body exceeded
            :data:`CONTAINER_MAX_RESPONSE_BYTES`.
    """
    raw = bytearray()
    while len(raw) <= CONTAINER_MAX_RESPONSE_BYTES:
        chunk = await response.content.read(CONTAINER_MAX_RESPONSE_BYTES + 1 - len(raw))
        if not chunk:
            return bytes(raw)
        raw += chunk
    raise ContainerUnreachableError(
        f"container response exceeded {CONTAINER_MAX_RESPONSE_BYTES} bytes"
    )


async def _read_json_capped(response: aiohttp.ClientResponse) -> Any:
    """Read + parse a JSON body under a hard size cap and content-type check."""
    ctype = response.content_type
    if ctype != _JSON_CONTENT_TYPE:
        raise ContainerUnreachableError(
            f"unexpected content-type from container endpoint: {ctype!r}"
        )
    raw = await _read_body_capped(response)
    import json  # noqa: PLC0415

    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as err:
        raise ContainerUnreachableError(
            "container response was not valid JSON"
        ) from err


async def _require_ack_deleted(response: aiohttp.ClientResponse) -> None:
    """Accept an ack answer only when the body states that the secret is gone.

    On this route the status alone decides nothing, because ``410`` covers two
    *opposite* situations (``docker-login/token_server.py``):

    * ``{"status": "already_deleted"}`` -- the idempotent success: a duplicate
      ack, or the TTL fallback deleted the file first. Nothing is left on disk.
      (``{"status": "deleted"}`` with ``200`` is the same success, one step
      earlier: this call is what removed the file.)
    * ``{"error": "locked"}`` -- the endpoint locked itself after five bad
      pairing-code presentations from *any* loopback client, which can happen
      between the fetch and the deferred ack. ``_run()`` deliberately **keeps**
      ``secrets.json`` in that case, so the credential is still on disk and the
      ack did not happen. Reporting that as success would hide a credential that
      then survives indefinitely -- there is no TTL delete after a lockout.

    Both accepted statuses are checked the same way on purpose: a rule that only
    scrutinises the ambiguous status would still let a foreign ``200`` (a proxy,
    a wrong port) count as a confirmed deletion. A body that is neither marker
    (unreadable, wrong content type, oversized, non-JSON, or an unknown payload)
    is a failure too: no classification, no proof of deletion. The negative path
    is explicit, never a silent success.

    This is the **only** place that sets
    :attr:`ContainerLoginError.secret_retained`, because the lockout body is the
    only evidence the client ever gets that the file survives. A ``401``/``403``
    from :func:`_raise_for_auth` shares this error class but not that fact: on
    the ack route a ``403`` is a wrong ``delete_token`` (which the server does
    not count towards the lockout) or a nonce that no longer matches (which it
    *does* count, so a stale ack aimed at a freshly restarted container eats
    that container's attempts). Either way the endpoint keeps running and its
    TTL deletes the file after all, so nothing is left behind.

    Raises:
        ContainerAuthError: the endpoint reported the lockout refusal
            (``secret_retained=True``).
        ContainerUnreachableError: the body could not be read/classified, so the
            deletion is unconfirmed.
    """
    payload = await _read_json_capped(response)
    if isinstance(payload, dict):
        if payload.get("status") in _ACK_DELETED_MARKERS:
            return
        if payload.get("error") == _LOCKED_MARKER:
            raise ContainerAuthError(
                "container endpoint is locked out (HTTP 410): too many bad "
                "pairing-code attempts; it kept the secret on disk, so the "
                "delete did NOT happen and no TTL delete follows",
                secret_retained=True,
            )
    raise ContainerUnreachableError(
        f"container ack returned HTTP {response.status} without confirming the deletion"
    )


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
        ContainerAuthError: the nonce was rejected (HTTP 401/403), or the
            endpoint has nothing left to hand out (HTTP 410: code expired,
            locked out, or the one-shot bundle was already collected). Note the
            asymmetry with :func:`ack_consumed`, where the same status is
            decided by the response body instead.
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
            # Fetch path: 410 means the bundle is unobtainable with this code.
            _raise_for_auth(response.status, gone_is_auth=True)
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
    accepted the bundle. A failure here is non-fatal for the caller -- usually
    the container still deletes on its own TTL fallback -- but is surfaced as a
    typed error so the caller can log it (never the token itself). The one
    failure that does *not* end in a TTL delete is the lockout below: there the
    server keeps the file deliberately.

    Success is decided by the response **body**, not by the status, unlike on
    the fetch path where the status alone is an auth failure (see
    :func:`_raise_for_auth`). ``{"status": "deleted"}`` (200) and
    ``{"status": "already_deleted"}`` (410) confirm the deletion; the server
    sends that same 410 with ``{"error": "locked"}`` after a lockout while
    *keeping* the secret on disk. That case, and every unclassifiable body, is
    raised rather than booked as a completed delete (see
    :func:`_require_ack_deleted`). Only the lockout carries
    ``secret_retained=True``; callers must branch on that flag rather than on
    the error class, which the plain ``401``/``403`` rejection shares without
    keeping the file.

    Whether a raised error is retried or the cleanup job is retained is the
    caller's decision; this function only refuses to report an unconfirmed
    delete as done.

    Raises:
        ContainerAuthError: the endpoint rejected the request (401/403 -- on
            this route a 403 is usually a wrong ``delete_token``, less often a
            bad nonce; ``secret_retained=False``, the TTL delete still follows),
            or answered the 410 with its lockout body
            (``secret_retained=True``).
        ContainerTimeoutError: the request timed out.
        ContainerUnreachableError: connection/redirect failure, an unexpected
            status, or a body that does not confirm the deletion.
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
            # ACK path: 410 is not an auth failure by status alone -- the body
            # decides (idempotent success vs. lockout), see _require_ack_deleted.
            _raise_for_auth(response.status, gone_is_auth=False)
            if response.status not in (_HTTP_OK, _HTTP_GONE):
                raise ContainerUnreachableError(
                    f"container ack returned HTTP {response.status}"
                )
            # Neither of the two remaining statuses is a success on its own: the
            # server states the outcome in the body, and 410 doubles as the
            # lockout refusal that keeps the secret on disk.
            await _require_ack_deleted(response)
    except (aiohttp.ServerTimeoutError, TimeoutError) as err:
        # aiohttp raises the builtin TimeoutError on a total timeout; it is not a
        # ServerTimeoutError nor a ClientError subclass, so catch it explicitly.
        raise ContainerTimeoutError(str(err)) from err
    except aiohttp.ClientError as err:
        raise ContainerUnreachableError(str(err)) from err


def _raise_for_auth(status: int, *, gone_is_auth: bool) -> None:
    """Map an auth-class status to :class:`ContainerAuthError`.

    ``401``/``403`` are an auth failure on every path. ``410`` is deliberately
    **asymmetric** and the caller must say which side it is on:

    * ``gone_is_auth=True`` (fetch path): the endpoint has nothing left to hand
      out. The pairing code expired, was locked out after too many attempts, or
      the bundle was already delivered (the server is one-shot). The user has to
      run the login container again and use the fresh code, so this is an auth
      failure, not an unreachable host. Mapping it to
      :class:`ContainerUnreachableError` would show a misleading host/port error.
      This is the only case that sets ``ContainerAuthError.code_used=True``, so
      the caller can tell "code already used/expired" apart from "wrong code".
    * ``gone_is_auth=False`` (ack path): ``410`` is ambiguous and cannot be
      decided from the status at all, so this function stays out of it. The body
      tells "already deleted" (success) apart from "locked" (the secret is still
      on disk); :func:`_require_ack_deleted` makes that call. Deciding it here
      would either swallow a lockout or turn a completed delete into a spurious
      auth failure.

    ``401``/``403`` keep ``code_used=False``: there the code itself was wrong,
    and re-typing it is the useful advice. They also keep
    ``secret_retained=False`` on both paths: a rejected request tells nothing
    about the on-disk file, and on the ack route the endpoint stays up and
    deletes on its TTL as usual. Only :func:`_require_ack_deleted` ever sets
    that flag.
    """
    if status == _HTTP_GONE and gone_is_auth:
        raise ContainerAuthError(
            "container endpoint has no bundle to hand out (HTTP 410): the pairing "
            "code expired, was locked out, or the bundle was already collected; "
            "run the login container again for a fresh code",
            code_used=True,
        )
    if status in (401, 403):
        raise ContainerAuthError(
            f"container endpoint rejected the pairing code (HTTP {status})"
        )

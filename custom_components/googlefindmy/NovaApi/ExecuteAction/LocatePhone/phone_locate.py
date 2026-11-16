# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_locate.py
"""Locate a phone that Google lists without a canonical ID (phone action 4).

Order of one request, which the push timing dictates (the answer arrives about
0.6 s after the POST): the register entry with the fresh key and the push callback
exist BEFORE the request is built and sent; then one ``async_nova_request`` without
retries; then the wait for the push, at most ``LOCATION_REQUEST_TIMEOUT_S``; then
the decryption inside the register. Entry and callback are removed in ``finally``.

Battery level and Wi-Fi name of the answering push go to ``status_sink``
before the location is decrypted, so a push without a usable location still
updates them. They never travel in the location row: the location cache drops a
row whose fix is older than the stored one, and the status of that push is
fresh all the same.

What this path deliberately does not do:

* It never repeats a failed request: ``max_retries=0`` on the
  transport turns off the retries for 408, 429, 5xx and network failures, and a
  timeout returns ``[]`` so the coordinator asks again only in its next cycle.
  After a 401 the transport resends at most once (``max_auth_resends=
  PHONE_MAX_AUTH_RESENDS``: one token refresh, 6 s, one resend); a request
  refused with 401 never reached the phone, and a further 401 ends as a
  transient ``NovaAuthError``.
* It never touches the receiver's locate-sent stamp or its dead-token detector
  (``async_register_for_location_updates``, ``note_accepted_without_push``): a
  silent phone is not evidence about the push token, and a phone that is switched
  off must not cost the account a token re-registration.
* It never falls back to another entry's push token: the token comes from
  ``receiver.get_entry_fcm_token(entry_id)`` or the request is not sent.
* It does not choose the key: the register creates it per request.

A request that does not reach the server, or that the server refuses with a 429
or 5xx, raises ``LocationRequestNotAcceptedError`` with one of the stages in
``PHONE_LOCATE_NOT_ACCEPTED_STAGES`` (same contract as the tracker path, own
markers); a ``NovaAuthError`` passes through unchanged so the API layer can map it.

Measurement channel: each sent request writes one DEBUG line ``Phone
action=%d trigger=%s entry=%s`` and calls the action observer, which the
coordinator binds to its ``phone_action_<n>_<auto|manual>`` counters. Log records
of this module carry the entry id, a stage or a status, never the Android ID, the
key, the payload, coordinates or the Wi-Fi name.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol, cast

import aiohttp

from custom_components.googlefindmy.const import (
    LOCATION_REQUEST_TIMEOUT_S,
    PHONE_MAX_AUTH_RESENDS,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_action_request import (
    PhoneAction,
    build_phone_action_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_location_crypto import (
    PhoneFix,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_request_registry import (
    PhoneRequestRegistry,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_status import (
    PhoneStatus,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    HTTP_TOO_MANY_REQUESTS,
    NovaAuthError,
    NovaError,
    NovaHTTPError,
    NovaRateLimitError,
    async_nova_request,
    current_dispatch_kind,
)
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid

_LOGGER = logging.getLogger(__name__)

PHONE_LOCATE_NOT_ACCEPTED_STAGES: frozenset[str] = frozenset(
    {
        "phone_target_unknown",  # raised by the API layer: no Android ID for the id
        "phone_no_entry_scope",  # raised by the API layer: no entry for the push channel
        "phone_receiver_unavailable",
        "phone_fcm_not_ready",  # raised by the API layer: push channel not listening
        "phone_no_fcm_token",
        "phone_request_open",
        "phone_rate_limited",
        "phone_server_error",
        "phone_network_error",
        "phone_request_failed",
    }
)

PhoneActionObserver = Callable[[int, str], None]
# Receives ``(phone_id, status)`` once per answering push that carries a status.
PhoneStatusSink = Callable[[str, PhoneStatus], None]


class PhoneLocateReceiver(Protocol):
    """What the push receiver offers the phone path (``FcmReceiverHA``)."""

    def get_entry_fcm_token(self, entry_id: str) -> str | None:
        """Return the push token of ``entry_id`` alone, or ``None``."""

    def register_phone_request(
        self, entry_id: str, request_uuid: str, callback: Callable[[str], None]
    ) -> None:
        """Route the push that answers ``request_uuid`` to ``callback``."""

    def unregister_phone_request(self, entry_id: str, request_uuid: str) -> None:
        """Forget the phone request ``request_uuid`` of ``entry_id``."""


def _receiver_token(receiver: object, entry_id: str) -> str | None:
    """Return the entry's own push token, strictly scoped, or ``None``."""
    getter = getattr(receiver, "get_entry_fcm_token", None)
    if not callable(getter):
        return None
    token = getter(entry_id)
    return token if isinstance(token, str) and token.strip() else None


def phone_location_row(fix: PhoneFix, *, phone_id: str) -> dict[str, Any]:
    """Return the location row the coordinator consumes for one phone fix.

    The accuracy is carried only when the phone reported one (``PhoneFix``
    leaves it ``None`` otherwise); the cache then applies its fallback radius
    with ``accuracy_estimated=True``. The phone reports its own position,
    so the row is an own report.
    """
    return {
        "canonic_id": phone_id,
        "latitude": fix.latitude,
        "longitude": fix.longitude,
        "altitude": None,
        "accuracy": fix.accuracy,
        "last_seen": fix.timestamp,
        "status": None,
        "status_code": None,
        "is_own_report": True,
        "is_network_report": False,
        "semantic_name": None,
        "locate_route": "phone",
    }


def _not_accepted(
    entry_id: str, stage: str, *, status: int | None = None
) -> LocationRequestNotAcceptedError:
    """Log the failure once (entry and stage only) and build the signal."""
    _LOGGER.warning(
        "Phone location request for entry %s not accepted (stage=%s%s)",
        entry_id,
        stage,
        "" if status is None else f", status={status}",
    )
    return LocationRequestNotAcceptedError(stage=stage, status=status)


async def _send_locate_request(  # noqa: PLR0913 - one argument per collaborator
    entry_id: str,
    payload: str,
    *,
    session: aiohttp.ClientSession | None,
    cache: Any,
    namespace: str | None,
    action_observer: PhoneActionObserver | None,
) -> None:
    """POST the built locate request once; map every refusal to the signal.

    Writes the measurement line and calls the observer immediately before the
    POST (the request is counted as sent when it leaves for the wire, like the
    locate clock of the coordinator).
    """
    trigger = current_dispatch_kind() or "unknown"
    _LOGGER.debug(
        "Phone action=%d trigger=%s entry=%s",
        int(PhoneAction.LOCATE),
        trigger,
        entry_id,
    )
    if action_observer is not None:
        try:
            action_observer(int(PhoneAction.LOCATE), trigger)
        except Exception as err:  # noqa: BLE001 - a counter must not cost the request
            _LOGGER.debug("Phone action observer failed: %s", type(err).__name__)
    try:
        await async_nova_request(
            NOVA_ACTION_API_SCOPE,
            payload,
            session=session,
            namespace=namespace,
            cache=cache,
            max_retries=0,
            max_auth_resends=PHONE_MAX_AUTH_RESENDS,
        )
    except asyncio.CancelledError:
        raise
    except NovaRateLimitError as err:
        raise _not_accepted(
            entry_id, "phone_rate_limited", status=HTTP_TOO_MANY_REQUESTS
        ) from err
    except NovaHTTPError as err:
        raise _not_accepted(entry_id, "phone_server_error", status=err.status) from err
    except NovaAuthError:
        raise
    except NovaError as err:
        # The transport wraps a network failure that exhausted its attempts
        # (here: the single one) in NovaError with the aiohttp error as cause.
        if isinstance(err.__cause__, (aiohttp.ClientError, TimeoutError)):
            raise _not_accepted(entry_id, "phone_network_error") from err
        raise _not_accepted(entry_id, "phone_request_failed") from err
    except aiohttp.ClientError as err:
        raise _not_accepted(entry_id, "phone_network_error") from err
    except ValueError as err:
        # The lock on the send path or the transport's argument checks refused
        # the payload before anything went out (PhoneActionNotAllowedError is a
        # ValueError). No broad handler here on purpose: an unexpected error is
        # not disguised as a refused request; the API layer reports it.
        raise _not_accepted(entry_id, "phone_request_failed") from err


def _deliver_status(  # noqa: PLR0913 - one argument per collaborator
    registry: PhoneRequestRegistry,
    *,
    entry_id: str,
    request_uuid: str,
    push_hex: str,
    phone_id: str,
    status_sink: PhoneStatusSink | None,
) -> None:
    """Hand the push's battery level and Wi-Fi name to ``status_sink``, if any.

    A failing sink costs neither the location nor the cleanup: it is logged at
    DEBUG with the error type only (the status holds a Wi-Fi name).
    """
    if status_sink is None:
        return
    status = registry.read_status(entry_id, request_uuid, push_hex)
    if status is None:
        return
    try:
        status_sink(phone_id, status)
    except Exception as err:  # noqa: BLE001 - a sensor store must not cost the fix
        _LOGGER.debug("Phone status sink failed: %s", type(err).__name__)


async def locate_phone(  # noqa: PLR0913 - one argument per collaborator
    *,
    phone_id: str,
    android_id: int,
    entry_id: str,
    receiver: object,
    registry: PhoneRequestRegistry,
    session: aiohttp.ClientSession | None,
    cache: Any,
    namespace: str | None,
    timeout_s: float = LOCATION_REQUEST_TIMEOUT_S,
    action_observer: PhoneActionObserver | None = None,
    status_sink: PhoneStatusSink | None = None,
) -> list[dict[str, Any]]:
    """Send one locate request to the phone and return ``[row]`` or ``[]``.

    Args:
        phone_id: The phone's row id (``aphone-…``), class (b), may be logged.
        android_id: The phone's Android ID from the current device list; used
            for the request and the push check, never logged.
        entry_id: Config entry whose account and push channel are used.
        receiver: The push receiver; must offer ``get_entry_fcm_token``,
            ``register_phone_request`` and ``unregister_phone_request``.
        registry: The entry's register of open requests (holds the keys).
        session: Shared aiohttp session for the Nova request.
        cache: Entry-scoped token cache for the Nova request.
        namespace: Cache namespace of the entry.
        timeout_s: How long to wait for the push after the POST.
        action_observer: Called once per sent request with ``(4, trigger)``.
        status_sink: Called with ``(phone_id, status)`` for the answering push
            of the requested phone, before the location is decrypted and also
            when it does not decrypt; a status may carry neither value.

    Returns:
        A list with one location row when the phone answered with a location
        that decrypted, otherwise ``[]`` (timeout, or an answer that did not
        decrypt or named another phone).

    Raises:
        LocationRequestNotAcceptedError: The request was not sent or the server
            did not accept it; ``stage`` names the position
            (``PHONE_LOCATE_NOT_ACCEPTED_STAGES``).
        NovaAuthError: Passed through unchanged from the transport.
    """
    for name in ("register_phone_request", "unregister_phone_request"):
        if not callable(getattr(receiver, name, None)):
            raise _not_accepted(entry_id, "phone_receiver_unavailable")
    push_receiver = cast("PhoneLocateReceiver", receiver)
    token = _receiver_token(receiver, entry_id)
    if token is None:
        raise _not_accepted(entry_id, "phone_no_fcm_token")

    request_uuid = generate_random_uuid()
    loop = asyncio.get_running_loop()
    request = registry.open(
        entry_id=entry_id,
        request_uuid=request_uuid,
        phone_id=phone_id,
        android_id=android_id,
        timeout_s=timeout_s,
    )
    if request is None:
        _LOGGER.debug(
            "Phone %s of entry %s already has an open location request; "
            "this one is not sent",
            phone_id,
            entry_id,
        )
        raise _not_accepted(entry_id, "phone_request_open")

    try:
        push_receiver.register_phone_request(entry_id, request_uuid, request.deliver)
        payload = build_phone_action_request(
            android_id,
            PhoneAction.LOCATE,
            token,
            request_uuid,
            locate_key=request.key,
        )
        await _send_locate_request(
            entry_id,
            payload,
            session=session,
            cache=cache,
            namespace=namespace,
            action_observer=action_observer,
        )
        try:
            push_hex = await asyncio.wait_for(request.wait(), timeout=timeout_s)
        except TimeoutError:
            # DEBUG only: the coordinator reports a device that stays silent once
            # at INFO. No dead-token signal and no repeat.
            _LOGGER.debug(
                "No phone location push for entry %s within %ss", entry_id, timeout_s
            )
            return []

        _deliver_status(
            registry,
            entry_id=entry_id,
            request_uuid=request_uuid,
            push_hex=push_hex,
            phone_id=phone_id,
            status_sink=status_sink,
        )
        fix = await loop.run_in_executor(
            None, registry.decrypt_push, entry_id, request_uuid, push_hex
        )
        if fix is None:
            _LOGGER.debug(
                "Phone location push for entry %s did not yield a location", entry_id
            )
            return []
        return [phone_location_row(fix, phone_id=phone_id)]
    finally:
        try:
            push_receiver.unregister_phone_request(entry_id, request_uuid)
        finally:
            registry.close(entry_id, request_uuid)

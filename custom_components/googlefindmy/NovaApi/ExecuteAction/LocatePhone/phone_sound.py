# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_sound.py
"""Ring a phone listed without a canonical ID, or stop its ring (actions 1 and 5).

Both requests carry the same scope and metadata form as the locate request
(Android ID in scope field 1, type ANDROID, the entry's push token) and an
empty action message: no key, no other field (measured on a live phone). The
builder accepts only actions 1, 4 and 5, and the payload lock in
``async_nova_request`` checks the bytes again before the POST; this module
narrows that further to 1 and 5.

What this module deliberately does not do:

* It does not repeat a request the transport could retry: ``max_retries=0``
  turns off the retries for 408, 429, 5xx and network failures. After a 401
  the transport resends at most once (``max_auth_resends=
  PHONE_MAX_AUTH_RESENDS``: one token refresh, 6 s, one resend); a
  request refused with 401 never reached the phone, and a further 401 ends as
  a transient ``NovaAuthError``.
* It never parses the answer. Like the tracker ring, an HTTP 200 means Nova
  accepted the request, not that the phone rang or went quiet
  (IRR-CA-NO-RING-CONFIRMATION in ``docs/PLAY_SOUND_ARCHITECTURE.md``).
* It never checks readiness or picks a token: the API layer asks the push
  receiver first and passes only the entry's own token.

Errors from the transport pass through unchanged, so the API layer classifies
them with the same ladder as the tracker ring (``SoundDispatchOutcome``).

Measurement channel: each request handed to the transport writes one
DEBUG line ``Phone action=%d trigger=%s entry=%s`` and calls the action
observer (like the locate path, also when the payload lock or the network
then refuses it), which the
coordinator binds to its ``phone_action_<n>_manual`` counters. Log records of
this module carry the entry id and the action number, never the Android ID,
the token or the payload.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from custom_components.googlefindmy.const import PHONE_MAX_AUTH_RESENDS
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_action_request import (
    PhoneAction,
    build_phone_action_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_locate import (
    PhoneActionObserver,
)
from custom_components.googlefindmy.NovaApi.nova_request import async_nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE

_LOGGER = logging.getLogger(__name__)

PHONE_SOUND_ACTIONS: frozenset[PhoneAction] = frozenset(
    {PhoneAction.RING, PhoneAction.STOP_RING}
)

# Ring and stop are only ever started by a person (button or service call).
_SOUND_TRIGGER = "manual"


async def async_submit_phone_sound_request(  # noqa: PLR0913 - one argument per collaborator
    action: PhoneAction,
    android_id: int,
    fcm_token: str,
    request_uuid: str,
    *,
    entry_id: str,
    session: aiohttp.ClientSession | None,
    cache: Any,
    namespace: str | None,
    action_observer: PhoneActionObserver | None = None,
) -> str | None:
    """Send one ring (1) or stop-ring (5) request to the phone.

    Args:
        action: ``PhoneAction.RING`` or ``PhoneAction.STOP_RING``.
        android_id: The phone's Android ID from the current device list; never
            logged.
        fcm_token: The entry's own push token (metadata field 4); never logged.
        request_uuid: A fresh request UUID; for a ring it is the cancel key the
            API returns, for a stop it is new (the stop is device-scoped).
        entry_id: Config entry whose account sends the request (logged).
        session: Shared aiohttp session.
        cache: Entry-scoped token cache.
        namespace: Cache namespace of the entry.
        action_observer: Called once per request handed to the transport with
            ``(action, "manual")``.

    Returns:
        The hex body Nova returned (accepted), as ``async_nova_request`` does.

    Raises:
        ValueError: ``action`` is not 1 or 5 (nothing is built or sent), or the
            builder or the payload lock refused the request.
        NovaError: Every transport failure or refusal, unchanged.
    """
    if action not in PHONE_SOUND_ACTIONS:
        raise ValueError("phone sound path sends only actions 1 and 5")
    payload = build_phone_action_request(
        android_id, PhoneAction(action), fcm_token, request_uuid
    )
    _LOGGER.debug(
        "Phone action=%d trigger=%s entry=%s",
        int(action),
        _SOUND_TRIGGER,
        entry_id,
    )
    if action_observer is not None:
        try:
            action_observer(int(action), _SOUND_TRIGGER)
        except Exception as err:  # noqa: BLE001 - a counter must not cost the request
            _LOGGER.debug("Phone action observer failed: %s", type(err).__name__)
    return await async_nova_request(
        NOVA_ACTION_API_SCOPE,
        payload,
        session=session,
        namespace=namespace,
        cache=cache,
        max_retries=0,
        max_auth_resends=PHONE_MAX_AUTH_RESENDS,
    )

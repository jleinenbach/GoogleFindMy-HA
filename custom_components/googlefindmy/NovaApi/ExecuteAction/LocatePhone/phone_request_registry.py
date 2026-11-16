# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_request_registry.py
"""Open locate requests of phones listed without a canonical ID, with their keys.

One locate request (phone action 4) carries a fresh 16-byte key; the phone answers
with a push that names the ``requestUuid`` of that request and holds the location
encrypted under that key. This register binds the key to ``(entry_id,
request_uuid)`` for the lifetime of the request: it is created in :meth:`open`
before the request is sent (the push can arrive 0.6 s later), it is read only by
:meth:`decrypt_push`, and :meth:`close` drops it after the answer or the timeout.
:meth:`read_status` reads the unencrypted battery level and Wi-Fi name of the same
push, under the same Android ID check, without the key.
The key never leaves this module; the builder receives it through
:attr:`PhoneRequest.key` once, for the request itself.

A second index per ``(entry_id, phone_id)`` allows one open request per phone:
:meth:`open` answers ``None`` while a request for that phone is open, so a second
request is neither sent nor given a second key.

The register is changed only from the Home Assistant event loop; the push
receiver hands the raw push over through :meth:`PhoneRequest.deliver`, which is
safe to call from the loop and, through ``call_soon_threadsafe``, from another
thread.

Nothing here is logged with a key, an Android ID, a location or a Wi-Fi name.
"""

from __future__ import annotations

import asyncio
import binascii
import logging
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from google.protobuf.message import DecodeError

from .phone_location_crypto import PhoneFix, decrypt_phone_location, new_locate_key
from .phone_status import PhoneStatus, status_from_record

_LOGGER = logging.getLogger(__name__)


@dataclass(eq=False)
class PhoneRequest:
    """One open locate request of one phone.

    ``deadline`` is the monotonic time after which the caller stops waiting
    (``LOCATION_REQUEST_TIMEOUT_S`` after :meth:`PhoneRequestRegistry.open`).
    """

    entry_id: str
    request_uuid: str
    phone_id: str
    android_id: int = field(repr=False)
    deadline: float
    _key: bytes = field(repr=False)
    _future: asyncio.Future[str] = field(repr=False)
    _loop: asyncio.AbstractEventLoop = field(repr=False)

    @property
    def key(self) -> bytes:
        """The 16-byte key of this request (for the builder only)."""
        return self._key

    def deliver(self, push_hex: str) -> None:
        """Hand the raw push for this request over; later pushes are ignored."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._set_result(push_hex)
        else:
            self._loop.call_soon_threadsafe(self._set_result, push_hex)

    def _set_result(self, push_hex: str) -> None:
        if not self._future.done():
            self._future.set_result(push_hex)

    async def wait(self) -> str:
        """Return the raw push for this request once it has been delivered."""
        return await self._future


def _pb2() -> Any:
    return import_module(
        "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2"
    )


def _phone_record_for(push_hex: str, android_id: int) -> Any | None:
    """Return the push's phone record (``PhoneInformation``), or ``None``.

    ``None`` when the push is not a ``DeviceUpdate`` or when it names a
    different Android ID than the request (a push is read only for the
    phone it was requested from).
    """
    pb2 = _pb2()
    try:
        update = pb2.DeviceUpdate()
        update.ParseFromString(binascii.unhexlify(push_hex))
        metadata = update.deviceMetadata
        if metadata.identifierInformation.phoneInformation.phoneInformationField1 != (
            android_id
        ):
            return None
        record = pb2.DeviceMetadata.PhoneInformation()
        record.ParseFromString(metadata.phoneInformation)
    except (DecodeError, binascii.Error, ValueError, TypeError):
        return None
    return record


def _envelope_for(push_hex: str, android_id: int) -> bytes | None:
    """Return field 36 of the push's phone record, or ``None``.

    ``None`` as for :func:`_phone_record_for`, and when the phone record carries
    no location.
    """
    record = _phone_record_for(push_hex, android_id)
    if record is None:
        return None
    envelope = bytes(record.phoneInformationField36)
    return envelope or None


class PhoneRequestRegistry:
    """Open phone locate requests of one API instance (one config entry)."""

    def __init__(self) -> None:
        self._by_request: dict[tuple[str, str], PhoneRequest] = {}
        self._by_phone: dict[tuple[str, str], str] = {}

    def __len__(self) -> int:
        return len(self._by_request)

    def is_open(self, entry_id: str, phone_id: str) -> bool:
        """Tell whether a request for ``phone_id`` of ``entry_id`` is open."""
        return (entry_id, phone_id) in self._by_phone

    def get(self, entry_id: str, request_uuid: str) -> PhoneRequest | None:
        """Return the open request ``request_uuid`` of ``entry_id``, if any."""
        return self._by_request.get((entry_id, request_uuid))

    def open(
        self,
        *,
        entry_id: str,
        request_uuid: str,
        phone_id: str,
        android_id: int,
        timeout_s: float,
    ) -> PhoneRequest | None:
        """Create the request with a fresh key, or return ``None`` if one is open.

        The key comes from ``new_locate_key()`` here and nowhere else; a caller
        cannot pass one in. ``None`` also when ``request_uuid`` is already taken
        for ``entry_id``.
        """
        phone_key = (entry_id, phone_id)
        request_key = (entry_id, request_uuid)
        if phone_key in self._by_phone or request_key in self._by_request:
            return None
        running = asyncio.get_running_loop()
        request = PhoneRequest(
            entry_id=entry_id,
            request_uuid=request_uuid,
            phone_id=phone_id,
            android_id=android_id,
            deadline=time.monotonic() + float(timeout_s),
            _key=new_locate_key(),
            _future=running.create_future(),
            _loop=running,
        )
        self._by_request[request_key] = request
        self._by_phone[phone_key] = request_uuid
        return request

    def close(self, entry_id: str, request_uuid: str) -> None:
        """Drop the request and its key; a request that is not open is ignored."""
        request = self._by_request.pop((entry_id, request_uuid), None)
        if request is None:
            return
        phone_key = (entry_id, request.phone_id)
        if self._by_phone.get(phone_key) == request_uuid:
            del self._by_phone[phone_key]
        if not request._future.done():  # noqa: SLF001 - owner of the request
            request._future.cancel()  # noqa: SLF001

    def decrypt_push(
        self, entry_id: str, request_uuid: str, push_hex: str
    ) -> PhoneFix | None:
        """Decrypt the push for ``request_uuid`` with the key of that request.

        ``None`` when the request is not open, when the push names another
        Android ID than the request, carries no location, or does not decrypt.
        The key stays in this register. CPU-bound; callers run it in an executor.
        """
        request = self._by_request.get((entry_id, request_uuid))
        if request is None:
            _LOGGER.debug(
                "Phone location push for entry %s matched no open request", entry_id
            )
            return None
        envelope = _envelope_for(push_hex, request.android_id)
        if envelope is None:
            _LOGGER.debug(
                "Phone location push for entry %s carried no location for the "
                "requested phone",
                entry_id,
            )
            return None
        return decrypt_phone_location(envelope, request.key)

    def read_status(
        self, entry_id: str, request_uuid: str, push_hex: str
    ) -> PhoneStatus | None:
        """Return battery level and Wi-Fi name of the push for ``request_uuid``.

        ``None`` when the request is not open, or when the push names another
        Android ID than the request or does not parse. A push of the
        requested phone always yields a status, even one with neither value:
        it says the phone is not on Wi-Fi and reported no battery level, and the
        stored values must not outlive it. Independent of the location: a push
        whose location is missing or does not decrypt still yields its status.
        """
        request = self._by_request.get((entry_id, request_uuid))
        if request is None:
            return None
        record = _phone_record_for(push_hex, request.android_id)
        if record is None:
            return None
        return status_from_record(record, observed_at=time.time())

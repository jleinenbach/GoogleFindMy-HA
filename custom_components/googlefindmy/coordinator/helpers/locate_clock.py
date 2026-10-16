# custom_components/googlefindmy/coordinator/helpers/locate_clock.py
"""Per-device clock of location requests, read for ``poll_age_s``.

The EID resolver reports how long before an observation the integration last
handed a location request for the device to Google (``poll_age_s`` in
``FMDN_UWT_TRANSITION``). Both send sites (the poll cycle and the manual locate)
wrap their await of ``api.async_get_device_location`` in ``stamp_on_dispatch``.
The request layer calls the observer once, immediately before
``async_nova_request`` (``location_request.observe_locate_dispatch``), and only
that call writes the stamp.

The stamp therefore exists only for requests that were handed to the send call,
and it carries the moment of the hand-over, not the start of the attempt: push
registration runs first and can take 44 seconds or more on a reconnect. An
attempt that stops earlier writes nothing, whatever the reason (no push token, a
failed push registration, a configuration error, a failure while the request is
built, a time limit or a cancellation during the push registration). A reader
that runs while an attempt is still in its push phase sees the previous value,
so nothing it logs has to be taken back later.

A request that was handed over counts even if it failed afterwards, for example
with a network or server error: the stamp says that the integration sent a
request, not that Google received or answered it.

Two overlapping requests for the same device (a manual locate still in flight
while the poll cycle asks again) each stamp at their own hand-over; the later
hand-over wins, because the observer belongs to the awaiting task.
"""

from __future__ import annotations

import time
from contextlib import AbstractContextManager

from ...NovaApi.ExecuteAction.LocateTracker.location_request import (
    observe_locate_dispatch,
)


def ensure_locate_stamps(existing: object) -> dict[str, float]:
    """Return ``existing`` if it is a ``dict``, else a new empty one.

    Coordinators built without ``__init__`` have no stamp mapping yet; both send
    sites go through this first.
    """
    if isinstance(existing, dict):
        return existing
    return {}


def stamp_on_dispatch(
    stamps: dict[str, float], device_id: str
) -> AbstractContextManager[None]:
    """Stamp ``device_id`` with ``time.monotonic()`` when the request inside is handed over."""

    def _stamp() -> None:
        stamps[device_id] = time.monotonic()

    return observe_locate_dispatch(_stamp)

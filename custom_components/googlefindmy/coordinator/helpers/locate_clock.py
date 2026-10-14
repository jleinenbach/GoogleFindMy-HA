"""Per-device clock of location requests, read for ``poll_age_s``.

The EID resolver reports how long before an observation the integration last
asked Google for a device's location (``poll_age_s`` in ``FMDN_UWT_TRANSITION``).
Both send sites (the poll cycle and the manual locate) stamp the device
immediately BEFORE they await the request, because a reply, or the tag's
reaction to it, can arrive while the request is still in flight.

A stamp set before the await cannot know whether the request will be sent at
all. Without a push token, or with a failed push registration, the request
layer stops before it calls Google, and the stamp would then claim a request
the server never received. ``retract_if_never_dispatched`` undoes the stamp for
exactly those stages and restores the value it replaced, so ``poll_age_s`` keeps
pointing at the last request that may have reached Google. Which stages count as
"never sent" is decided by the request layer
(``LocationRequestNotAcceptedError.before_dispatch``), not here.

Both functions work on the plain stamp mapping rather than on the coordinator,
so the mixins and the test stubs built from a single mixin share one
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)


@dataclass(frozen=True, slots=True)
class LocateStamp:
    """One stamp as set by a send site, with the value it replaced."""

    device_id: str
    stamp: float
    previous: float | None


def stamp_locate_attempt(
    stamps: dict[str, float], device_id: str, now: float
) -> LocateStamp:
    """Stamp ``device_id`` with ``now`` and return what is needed to undo it."""
    previous = stamps.get(device_id)
    stamps[device_id] = now
    return LocateStamp(device_id=device_id, stamp=now, previous=previous)


def retract_if_never_dispatched(
    stamps: dict[str, float],
    token: LocateStamp,
    error: LocationRequestNotAcceptedError,
) -> bool:
    """Undo ``token`` if ``error`` says the request was never sent to Google.

    Returns whether the stamp was undone. Nothing is undone when the request
    may have reached the server, or when the device's stamp no longer holds
    the value this attempt set (a later attempt, or a purge, replaced it; that
    value is not this attempt's to remove).
    """
    if not error.before_dispatch:
        return False
    if stamps.get(token.device_id) != token.stamp:
        return False
    if token.previous is None:
        del stamps[token.device_id]
    else:
        stamps[token.device_id] = token.previous
    return True

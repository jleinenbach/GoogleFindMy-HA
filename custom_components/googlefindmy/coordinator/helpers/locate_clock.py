# custom_components/googlefindmy/coordinator/helpers/locate_clock.py
"""Per-device clock of location requests, read for ``poll_age_s``.

The EID resolver reports how long before an observation the integration last
asked Google for a device's location (``poll_age_s`` in ``FMDN_UWT_TRANSITION``).
Both send sites (the poll cycle and the manual locate) stamp the device
immediately BEFORE they await the request, because a reply, or the tag's
reaction to it, can arrive while the request is still in flight. The stamp
therefore marks the start of the ATTEMPT, not the moment the request left the
host: push registration runs first and can take 44 seconds or more on a
reconnect (two waits for readiness of up to 22 seconds each, plus the client
and token setup, plus waiting for a reconnect that is already running).

A stamp set before the await cannot know whether the request will be sent at
all. When the request layer raises ``LocationRequestNotAcceptedError`` with
``before_dispatch`` set (no push token, a failed push registration, or a
failure while the request was being built), ``retract_if_never_dispatched``
takes exactly that attempt back. Which failures count as "never sent" is
decided by the request layer, which records whether ``async_nova_request`` was
called; it is not decided here and not by the stage name.

To take back exactly one attempt, ``LocateStamps`` keeps, next to the value
readers see, the attempts of each device that have not been taken back, each
with a serial number, plus the value the oldest of them replaced. The visible
value is always the newest attempt still on that list. This stays correct when
two attempts for the same device overlap (a manual locate still in its push
phase while the poll cycle asks again) and when two attempts share the same
clock reading. At most ``MAX_TRACKED_ATTEMPTS`` attempts are kept per device;
an older attempt is folded into the base value and can no longer be taken
back, which needs more overlapping attempts than the two send sites produce.

Not every attempt that never reached Google is taken back. These still count:

* the configuration errors the request layer raises before its outer handler
  (no push provider registered, a provider returning ``None``, a missing
  token cache, a failed import of the decrypt modules), which
  ``api.async_get_device_location`` turns into an empty result;
* a poll attempt cut off by the outer ``asyncio.wait_for`` while the push
  registration is still running, because a timeout carries no information
  about the send;
* an attempt cancelled while the push registration is still running (for
  example when the entry unloads), because ``asyncio.CancelledError`` passes
  through unchanged.

``poll_age_s`` is therefore "time since the last attempt that was not shown to
have stopped before the send", not proof that Google received a request.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ...NovaApi.ExecuteAction.LocateTracker.location_request import (
    LocationRequestNotAcceptedError,
)

MAX_TRACKED_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class LocateStamp:
    """One attempt as stamped by a send site."""

    device_id: str
    stamp: float
    serial: int


class LocateStamps(dict[str, float]):
    """``device_id`` to the ``time.monotonic`` value of the last counted attempt.

    Readers use it as a plain mapping. Writing a value or removing a device from
    outside the two functions below (a purge, a test fixture) drops that
    device's attempt list, so a later retraction cannot restore a value that
    was replaced in the meantime. Every writing ``dict`` method does this:
    ``[]=``, ``del``, ``pop``, ``popitem``, ``clear``, ``update``, ``|=`` and
    ``setdefault``. A copy (``copy.copy``, ``copy.deepcopy``, ``pickle``)
    carries the values but no attempts, so nothing in a copy can be taken back;
    ``dict.copy`` and ``|`` return a plain ``dict``.
    """

    __slots__ = ("_attempts", "_base", "_serials")

    def __init__(self, initial: Iterable[tuple[str, float]] | None = None) -> None:
        """Start empty, or from the pairs of an existing mapping."""
        super().__init__(initial or ())
        self._attempts: dict[str, list[LocateStamp]] = {}
        self._base: dict[str, float | None] = {}
        self._serials = itertools.count(1)

    def __setitem__(self, key: str, value: float) -> None:
        """Set a value from outside and forget the device's attempts."""
        self._forget(key)
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        """Remove a device and forget its attempts."""
        self._forget(key)
        super().__delitem__(key)

    def pop(self, key: str, *default: Any) -> Any:
        """Remove a device, as ``dict.pop`` does, and forget its attempts."""
        self._forget(key)
        return super().pop(key, *default)

    def popitem(self) -> tuple[str, float]:
        """Remove the last device, as ``dict.popitem`` does, and forget its attempts."""
        key, value = super().popitem()
        self._forget(key)
        return key, value

    def clear(self) -> None:
        """Remove every device and every attempt."""
        self._attempts.clear()
        self._base.clear()
        super().clear()

    def update(self, *args: Any, **kwargs: float) -> None:
        """Write values from outside, as ``dict.update`` does, and forget their attempts."""
        values: dict[str, float] = dict(*args, **kwargs)
        for key in values:
            self._forget(key)
        super().update(values)

    def __ior__(self, other: Any) -> LocateStamps:  # type: ignore[override,misc]
        """``|=`` writes from outside like ``update``."""
        self.update(other)
        return self

    def setdefault(self, key: str, default: float | None = None) -> Any:
        """Write ``default`` only for a missing device, forgetting stale attempts."""
        if key not in self:
            self._forget(key)
        return super().setdefault(key, default)  # type: ignore[arg-type]

    def __reduce__(self) -> tuple[type[LocateStamps], tuple[list[tuple[str, float]]]]:
        """Copy and pickle the values, not the attempts.

        ``copy.copy`` and ``copy.deepcopy`` go through this as well, so a copy
        never shares the attempt lists of the original.
        """
        return (LocateStamps, (list(self.items()),))

    def _forget(self, key: str) -> None:
        self._attempts.pop(key, None)
        self._base.pop(key, None)


def ensure_locate_stamps(existing: object) -> LocateStamps:
    """Return ``existing`` if it is a ``LocateStamps``, else a new one from it.

    Coordinators built without ``__init__`` have no stamp mapping yet, and test
    fixtures may set a plain dict; both send sites go through this first.
    """
    if isinstance(existing, LocateStamps):
        return existing
    if isinstance(existing, dict):
        return LocateStamps(existing.items())
    return LocateStamps()


def stamp_locate_attempt(
    stamps: LocateStamps, device_id: str, now: float
) -> LocateStamp:
    """Stamp ``device_id`` with ``now`` and return the token that can undo it."""
    attempts = stamps._attempts.setdefault(device_id, [])
    if not attempts:
        stamps._base[device_id] = dict.get(stamps, device_id)
    token = LocateStamp(device_id=device_id, stamp=now, serial=next(stamps._serials))
    attempts.append(token)
    if len(attempts) > MAX_TRACKED_ATTEMPTS:
        stamps._base[device_id] = attempts.pop(0).stamp
    dict.__setitem__(stamps, device_id, now)
    return token


def retract_if_never_dispatched(
    stamps: LocateStamps,
    token: LocateStamp,
    error: LocationRequestNotAcceptedError,
) -> bool:
    """Undo ``token`` if ``error`` says the request was never sent to Google.

    Returns whether the attempt was taken back. Nothing is undone when the
    request may have reached the server, or when the attempt is no longer on
    the device's list (a purge or an outside write replaced it, or it was
    folded into the base value).
    """
    if not error.before_dispatch:
        return False
    attempts = stamps._attempts.get(token.device_id)
    if not attempts:
        return False
    remaining = [attempt for attempt in attempts if attempt.serial != token.serial]
    if len(remaining) == len(attempts):
        return False
    if remaining:
        stamps._attempts[token.device_id] = remaining
        dict.__setitem__(stamps, token.device_id, remaining[-1].stamp)
        return True
    del stamps._attempts[token.device_id]
    base = stamps._base.pop(token.device_id, None)
    if base is None:
        dict.pop(stamps, token.device_id, None)
    else:
        dict.__setitem__(stamps, token.device_id, base)
    return True

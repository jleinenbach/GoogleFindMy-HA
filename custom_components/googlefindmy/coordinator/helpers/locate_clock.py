# custom_components/googlefindmy/coordinator/helpers/locate_clock.py
"""Per-device clock of location requests, read for ``poll_age_s``.

The EID resolver reports how long before an observation the integration last
sent a location request for the device (``poll_age_s`` in
``FMDN_UWT_TRANSITION``). Both send sites (the poll cycle and the manual locate)
wrap their await of ``api.async_get_device_location`` in ``stamp_on_dispatch``.
The transport calls the observer immediately before each ``session.post``
(``nova_request.observe_request_dispatch``), and only that call writes the
stamp.

The stamp therefore exists only for requests that were about to leave the host,
and it carries that moment, not the start of the attempt: push registration and
the token fetch run first, and together they can take a minute or more. An
attempt that stops earlier writes nothing, whatever the reason (no push token, a
failed push registration, a failure while the payload is built, a failed token
fetch, a time limit or a cancellation before the POST). A reader that runs while
an attempt is still in one of these phases sees the previous value, so nothing
it logs has to be taken back later.

A request that was POSTed counts even if it failed afterwards, for example with
a network or server error: the stamp says that the integration sent a request,
not that Google received or answered it. A retry stamps again.

Two overlapping requests for the same device (a manual locate still in flight
while the poll cycle asks again) each stamp at their own POST, because each
send site sets its own observer in its own context; the later POST wins. The
observer stops writing when its block ends, so a task that was created inside
the block and outlives it (the push receiver's supervisor is started on the
first locate) cannot stamp the device later.

A device purged while its request is still before the POST gets a stamp again
at the POST. The value then refers to a request that was really sent for that
id; it is accepted rather than guarded, because the purge runs only when a
device is deleted.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from ...NovaApi.nova_request import observe_request_dispatch


def ensure_locate_stamps(existing: object) -> dict[str, float]:
    """Return ``existing`` if it is a ``dict``, else a new empty one.

    Coordinators built without ``__init__`` have no stamp mapping yet; both send
    sites go through this first.
    """
    if isinstance(existing, dict):
        return existing
    return {}


@contextmanager
def stamp_on_dispatch(stamps: dict[str, float], device_id: str) -> Iterator[None]:
    """Stamp ``device_id`` with ``time.monotonic()`` at each POST inside the block."""
    is_open = True

    def _stamp() -> None:
        if is_open:
            stamps[device_id] = time.monotonic()

    try:
        with observe_request_dispatch(_stamp):
            yield
    finally:
        is_open = False

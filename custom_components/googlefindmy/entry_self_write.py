# custom_components/googlefindmy/entry_self_write.py
"""One-shot receipts that tell a receiver self-write apart from a user change.

The FCM receiver writes a renewed registration back into the entry's secrets
bundle, so the next setup does not re-seed the registration it replaced. That
write goes through ``async_update_entry`` and therefore reaches the entry's
update listener, which reloads the entry whenever its credential fingerprint
changes. A reload is wrong here: the receiver already runs on the new value,
and tearing it down would only repeat the handshake.

Excluding the member from the fingerprint would hide a *user* change of the
same member as well (an imported ``secrets.json`` that differs only there).
Instead, the receiver leaves a receipt before it writes: the digest of exactly
the value it is about to store. The listener consumes the receipt on the next
fingerprint change and skips the reload only when everything else is unchanged
and the stored member matches the receipt. Any other change reloads once.

A leaf module: it imports nothing from this package except constants, so the
receiver (``Auth``) and the listener (package ``__init__``) can both import it
without a cycle, like ``entry_reload_gate``.

Only digests are kept. ``hass.data`` holds no credential text
(``tests/test_hass_data_layout.py``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import MutableMapping
from typing import Any, Final

from .const import DOMAIN

SELF_WRITE_RECEIPTS_KEY: Final[str] = "fcm_self_write_receipts"
"""Key of the receipt map in ``hass.data[DOMAIN]`` (entry_id -> digest)."""


def credential_member_digest(value: Any) -> str:
    """Return a non-reversible digest of one credential member.

    Serialized like the entry's credential fingerprint (sorted keys, ``repr``
    for anything JSON cannot encode), so the receiver and the listener derive
    the same digest from the same value regardless of key order.
    """

    material = json.dumps(value, sort_keys=True, default=repr)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _receipts(hass: Any, *, create: bool) -> dict[str, str] | None:
    """Return the receipt map, creating it on demand; ``None`` without storage."""

    data = getattr(hass, "data", None)
    if not isinstance(data, MutableMapping):
        return None
    bucket = data.setdefault(DOMAIN, {}) if create else data.get(DOMAIN)
    if not isinstance(bucket, MutableMapping):
        return None
    receipts = bucket.get(SELF_WRITE_RECEIPTS_KEY)
    if isinstance(receipts, dict):
        return receipts
    if not create:
        return None
    receipts = {}
    bucket[SELF_WRITE_RECEIPTS_KEY] = receipts
    return receipts


def note_self_write(hass: Any, entry_id: str, digest: str) -> bool:
    """Leave the receipt for a write about to happen; ``False`` without storage.

    Only the most recent receipt per entry is kept: a second write before the
    listener ran replaces the first, and the listener then compares against the
    value that is actually stored.
    """

    receipts = _receipts(hass, create=True)
    if receipts is None:
        return False
    receipts[entry_id] = digest
    return True


def discard_self_write(hass: Any, entry_id: str) -> None:
    """Drop the receipt of ``entry_id`` (write failed, setup, removal)."""

    receipts = _receipts(hass, create=False)
    if receipts is not None:
        receipts.pop(entry_id, None)


def consume_self_write(hass: Any, entry_id: str) -> str | None:
    """Return and remove the receipt of ``entry_id``; ``None`` when there is none."""

    receipts = _receipts(hass, create=False)
    if receipts is None:
        return None
    return receipts.pop(entry_id, None)

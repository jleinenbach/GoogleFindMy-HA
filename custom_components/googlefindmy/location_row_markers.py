"""Transient markers on a location row, and the one substitution that sets one.

Why a top-level module rather than ``coordinator/helpers/cache.py``: the push
receiver in ``Auth/`` needs both functions, and importing them from the
coordinator package pulls ``coordinator/__init__`` (and with it ``main`` and
``polling``) into a module that ``polling`` itself imports a constant from. That
is a circular-import trap waiting for the first import order that hits it, and
``Auth/AGENTS.md`` asks for exactly this remedy: define a shared utility once in
a dependency-light shared module and import it at module scope.

Both functions are pure dict operations. They import nothing from this package.
"""

from __future__ import annotations

from typing import Any

# A leading underscore is this project's convention for a key that belongs to the
# journey of a payload, not to the row that gets cached: the fusion's
# ``_fusion_preapplied``, the deferred round-trip anchor intents, the accuracy
# tally marker, the substituted-radius marker.
TRANSIENT_KEY_PREFIX = "_"


def strip_transient_keys(row: dict[str, Any]) -> None:
    """Remove every transient marker from a row about to be cached.

    Two direct-write fallbacks exist (the poll cycle's test-double branch and the
    push receiver's), and neither CONSUMES a marker the way
    ``update_device_cache`` does - they only have to keep them out of the cache,
    from where they would surface as entity attributes.

    Naming the markers individually is what failed twice: each list was correct
    when it was written and wrong as soon as a marker was added
    (``_accuracy_counted`` leaked through both fallbacks that way). The prefix is
    the predicate, so nothing has to be maintained.
    """
    for key in [k for k in row if k.startswith(TRANSIENT_KEY_PREFIX)]:
        row.pop(key, None)


def substitute_zone_accuracy(row: dict[str, Any], radius: float) -> None:
    """Put a ZONE radius into ``accuracy`` and say that it is one.

    The Google Home filter replaces a detection at a Home speaker with the home
    zone's coordinates and its radius. That radius describes a zone; it is not a
    measurement of the device, and downstream it is byte-for-byte the same number
    as a genuinely coarse fix - which is why the accuracy gate would otherwise
    weigh it against the cached precision and refuse the filter's deliberate move
    home (a home zone of 200 m or more is enough).

    Only the substituting site knows the provenance, and there are three of them
    (poll, manual locate, push), so the rule lives here rather than three times
    over. The marker is transient: every write path pops it before the row
    reaches the cache.
    """
    row["accuracy"] = radius
    row["_accuracy_substituted"] = True

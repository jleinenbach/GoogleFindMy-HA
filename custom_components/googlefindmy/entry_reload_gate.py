# custom_components/googlefindmy/entry_reload_gate.py
"""State gate for the shared entry-reload latch.

Both questions about an entry and the latch live here, next to the latch
itself rather than in the config flow, because they have **two** claimants:
`config_flow` and the tracker registry self-heal in `device_tracker`. A leaf
module keeps that shared: it imports nothing from this package, so either
claimant can import it first without an import cycle, and neither has to reach
through the other.

The pair travels together on purpose. `entry_reload_is_hopeless` (may this
entry be promised a reload at all?) and `falsy_reload_left_the_latch_behind`
(did a falsy reload leave our claim standing?) share the entry lookup, the
`ConfigEntryState` binding and the `SOURCE_IGNORE` fallback; splitting them
would duplicate all three and let the two answers drift apart.

Everything here fails **open**, in the same direction as the latch itself: one
reload too many is a nuisance, a missing one leaves freshly written credentials
ineffective.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

TERMINAL_ENTRY_RELOAD_STATES: Final[frozenset[ConfigEntryState]] = frozenset(
    state
    for state in (
        getattr(ConfigEntryState, "MIGRATION_ERROR", None),
        getattr(ConfigEntryState, "FAILED_UNLOAD", None),
    )
    if state is not None
)
"""Entry states a reload can no longer come back from.

A **positive list of terminal states**, not the negation of an "is recoverable"
property. Home Assistant reports ``recoverable is False`` for four states, but
only two of them are terminal: ``MIGRATION_ERROR`` and ``FAILED_UNLOAD`` stay
that way without outside help, while ``SETUP_IN_PROGRESS`` and
``UNLOAD_IN_PROGRESS`` heal within seconds. ``async_reload`` enters
``entry.setup_lock``, which the running setup holds, so a reload scheduled
during either transient state waits and then runs against a state that is
recoverable again. Standing down for them would drop a legitimate reload -- and
with it freshly written credentials -- which is the very damage this latch
exists to prevent, only with the sign flipped. A future, unknown state is not in
this set either and therefore keeps today's behaviour.

The members are resolved from the imported symbols rather than spelled as
strings, and a name that cannot be resolved drops out of the set instead of
raising: an upstream rename shrinks the set (fail-open, reload as before)
instead of breaking the flow. ``tests/test_entry_reload_gate.py`` pins that the
set did not silently run empty.
"""


def resolve_config_entry(hass: HomeAssistant, entry_id: str) -> Any | None:
    """Look ``entry_id`` up through the entry manager, ``None`` when that fails.

    One place for the lookup, so the state gate and its caller cannot disagree
    on which manager method to use or on how a missing one is treated. Every
    doubt ends in ``None``; the callers read that as "no information" and carry
    on rather than standing down.

    ``async_get_entry`` and not ``async_get_known_entry``: it is the way already
    established in this integration, it returns ``None`` instead of raising
    ``UnknownEntry``, and the test stub carries it.

    No guard against an empty ``entry_id`` here: the only callers rule that out
    before they get this far, and the manager answers ``None`` for it anyway. A
    second guard would be a branch no test can reach, and an unreachable guard
    is not a safety net, it is a blind spot in the coverage report.
    """

    resolve = getattr(getattr(hass, "config_entries", None), "async_get_entry", None)
    if not callable(resolve):
        return None
    try:
        return resolve(entry_id)
    except Exception:  # noqa: BLE001 - a lookup must never block a reload
        _LOGGER.debug(
            "Could not resolve entry %s; treating it as unknown",
            entry_id,
            exc_info=True,
        )
        return None


def entry_reload_is_hopeless(
    hass: HomeAssistant, entry_id: str, entry: Any | None = None
) -> bool:
    """Whether reloading ``entry_id`` cannot possibly reach a setup.

    Claiming the shared latch is a *promise to reload*, and the release points
    (unload, setup, entry removal) all presuppose that a reload actually arrives.
    Where it cannot, the promise must not be made in the first place: the reload
    itself runs in a core-owned task that ``async_schedule_reload`` neither
    returns nor exposes, so nothing would hand the latch back and every later
    reload of that entry would stand down -- with its credentials ineffective --
    until the process restarts.

    Three cases qualify, all measured against the installed core:

    * a **terminal state** (``TERMINAL_ENTRY_RELOAD_STATES``): the reload can
      only raise ``OperationNotAllowed``;
    * a **disabled** entry: ``async_reload`` returns right after the unload
      (``if not unload_result or entry.disabled_by: return unload_result``) and
      never calls ``async_setup``;
    * an **ignored** entry (``SOURCE_IGNORE``): ``ConfigEntry.async_setup``
      bails out immediately (``if self.source == SOURCE_IGNORE or
      self.disabled_by: return``).

    The last two are invisible from the outside: the state stays recoverable and
    the result is truthy, so neither the state nor the return value of the reload
    tells them apart from a reload that landed.

    Fails **open** on every doubt -- an empty id, no resolver, a resolver that
    raises or returns ``None``, an entry without a state -- because one reload
    too many is a nuisance while a missing one leaves written credentials
    ineffective. That is the same direction the whole latch fails towards.

    ``entry`` may be passed when the caller already holds the object; that skips
    the second lookup and, more importantly, keeps the check from depending on
    whether a given manager happens to offer ``async_get_entry``. The tracker
    registry self-heal relies on that: it holds its ``ConfigEntry`` and its
    platform double carries no entry manager at all.
    """

    if not entry_id:
        return False

    if entry is None:
        entry = resolve_config_entry(hass, entry_id)
    if entry is None:
        return False

    if getattr(entry, "disabled_by", None):
        return True

    if getattr(entry, "source", None) == getattr(
        config_entries, "SOURCE_IGNORE", "ignore"
    ):
        return True

    state = getattr(entry, "state", None)
    if state is None:
        return False

    return state in TERMINAL_ENTRY_RELOAD_STATES


def falsy_reload_left_the_latch_behind(
    hass: HomeAssistant, entry_id: str, entry: Any | None = None
) -> bool:
    """Whether a falsy ``async_reload`` result means no release point ran.

    ``async_reload`` reports **at least four** different outcomes with the same
    falsy value, and they do not all mean the same thing for the latch:

    * the **unload failed** -- our ``async_unload_entry`` ran and its ``finally``
      handed the latch back already;
    * the **entry setup returned ``False``** -- our ``async_setup_entry`` ran and
      its head handed the latch back already;
    * the **component could not be set up at all** -- the reload took the
      ``entry.domain not in hass.config.components`` short circuit, so neither of
      our lifecycle hooks ran and the claim is still held;
    * ``ConfigEntry.async_setup`` **bailed before reaching our hook** and left
      the entry unloaded, which the manager reports as ``entry.state is not
      LOADED``, hence falsy. Two of its exits are unambiguous: an **ignored**
      entry (``if self.source == SOURCE_IGNORE or self.disabled_by: return``, the
      very first statement) and a **failed migration**
      (``MIGRATION_ERROR``). In both the domain stays among the loaded
      components, so the component check alone would misread them as released.

    Treating the first two as dead ends would discard a claim someone else may
    have taken in the meantime, which is the very double reload this latch
    exists to prevent, and it would blame the credentials for a reload that
    actually happened.

    ``SETUP_ERROR`` is deliberately **not** on the list: our own
    ``async_setup_entry`` reaches that state too, after its head has already
    released the latch, so the state cannot tell the two apart. ``disabled_by``
    is not on it either, for the same reason: with a falsy result it means the
    unload ran and failed, and that path released. Both stay residual rather
    than risking a release of somebody else's claim.

    Fails towards releasing, like everything else about this latch: where the
    component list or the entry cannot be read, one reload too many beats a
    promise nobody redeems. ``entry`` may be passed when the caller already holds
    the object, which keeps the answer from depending on whether a given manager
    happens to offer ``async_get_entry``.

    Callers must only ask this once they know the result is falsy. It is the one
    truth about that question for the paths that consult it; a second, private
    answer would drift.
    """

    if entry is None:
        entry = resolve_config_entry(hass, entry_id)

    # Unambiguous proof that ``ConfigEntry.async_setup`` never reached our hook.
    if getattr(entry, "source", None) == getattr(
        config_entries, "SOURCE_IGNORE", "ignore"
    ):
        return True
    # ``ConfigEntryState`` as imported at the top of this module, not
    # ``config_entries.ConfigEntryState``: the two are different objects under
    # the test stub, and ``TERMINAL_ENTRY_RELOAD_STATES`` uses this one.
    migration_error = getattr(ConfigEntryState, "MIGRATION_ERROR", None)
    if migration_error is not None and getattr(entry, "state", None) == migration_error:
        return True

    domain = getattr(entry, "domain", None) if entry is not None else None
    components = getattr(getattr(hass, "config", None), "components", None)
    if domain is not None and components is not None and domain in components:
        return False
    return True

# tests/helpers/core_shutdown_state.py
"""Seed the core attributes ``DataUpdateCoordinator.async_shutdown`` reads.

``GoogleFindMyCoordinator.async_shutdown`` chains to the core's shutdown. The
suite builds most coordinators with ``__new__`` and never runs
``DataUpdateCoordinator.__init__``, so the three attributes the core's shutdown
touches do not exist on such a double; a shutdown test would then fail on the
core's attribute reads instead of on the behaviour it is about. This helper
gives them the values the core's ``__init__`` gives them. Nothing else of the
core is mimicked: ``_debounced_refresh`` is a two-method stand-in for the
``Debouncer`` surface the core's shutdown uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def seed_core_shutdown_state[CoordinatorT](coordinator: CoordinatorT) -> CoordinatorT:
    """Return ``coordinator`` with the core's shutdown attributes in place.

    Attributes already set (a test wiring its own remover or debouncer stand-in
    to observe the core's calls) are left alone.
    """

    target: Any = coordinator
    if not hasattr(target, "_shutdown_requested"):
        target._shutdown_requested = False
    if not hasattr(target, "_unsub_refresh"):
        target._unsub_refresh = None
    if not hasattr(target, "_unsub_shutdown"):
        target._unsub_shutdown = None
    if not hasattr(target, "_debounced_refresh"):
        target._debounced_refresh = SimpleNamespace(
            async_shutdown=lambda: None, async_cancel=lambda: None
        )
    return coordinator

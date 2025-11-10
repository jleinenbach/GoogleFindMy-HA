# tests/helpers/config_flow.py
"""Config flow helpers shared across Google Find My tests."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any


def set_config_flow_unique_id(flow: Any, unique_id: str | None) -> None:
    """Assign ``unique_id`` on config flow stubs across HA variants.

    Home Assistant's runtime ``ConfigFlow`` exposes ``unique_id`` as a read-only
    property populated by :meth:`async_set_unique_id`. Our legacy stubs allowed
    direct assignment, so test scaffolding frequently sets the attribute to
    simulate partially-initialized flows. This helper keeps those tests
    compatible with both behaviors by storing the backing ``_unique_id`` field
    and mirroring Home Assistant's context bookkeeping regardless of whether the
    descriptor accepts assignment.
    """

    try:
        setattr(flow, "unique_id", unique_id)
    except AttributeError:
        # Modern Home Assistant implementations expose a read-only property.
        pass

    object.__setattr__(flow, "_unique_id", unique_id)

    context = getattr(flow, "context", None)
    if isinstance(context, MutableMapping):
        if unique_id is None:
            context.pop("unique_id", None)
        else:
            context["unique_id"] = unique_id

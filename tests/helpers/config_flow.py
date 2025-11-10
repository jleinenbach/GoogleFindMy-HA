# tests/helpers/config_flow.py
"""Config flow helpers shared across Google Find My tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

FlowInitResult = dict[str, Any]
FlowInitCallable = Callable[..., Awaitable[FlowInitResult] | FlowInitResult]


def config_entries_flow_stub(
    *,
    result: FlowInitResult | FlowInitCallable | None = None,
) -> SimpleNamespace:
    """Return a flow manager stub recording ``async_init`` invocations.

    The helper mirrors Home Assistant's ``ConfigEntries.flow`` contract well
    enough for tests that only need to verify invocation ordering. Each call to
    :func:`config_entries_flow_stub` returns a fresh object whose
    :meth:`async_init` method is an :class:`AsyncMock` storing the recorded
    positional and keyword arguments. When ``result`` is omitted, the stub
    returns a minimal form dictionary so callers can await the coroutine without
    additional scaffolding.
    """

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def _async_init(*args: Any, **kwargs: Any) -> FlowInitResult:
        calls.append((args, dict(kwargs)))
        resolved: FlowInitResult | FlowInitCallable | None = result
        if callable(resolved):
            candidate = resolved(*args, **kwargs)
            if inspect.isawaitable(candidate):
                candidate = await candidate
            return candidate
        if resolved is not None:
            return resolved
        return {"type": "form", "step_id": None, "errors": {}}

    return SimpleNamespace(async_init=AsyncMock(side_effect=_async_init), calls=calls)


def stub_async_entry_for_domain_unique_id(
    manager: Any, domain: str, unique_id: str
) -> Any | None:
    """Return the stored config entry matching ``domain``/``unique_id``.

    The Home Assistant core exposes :meth:`ConfigEntries.async_entry_for_domain_unique_id`
    to locate entries that already claimed a specific unique ID. The config-flow tests
    ship lightweight manager stubs instead of the production implementation, so this
    helper normalizes their storage patterns (single entry attributes, dictionaries,
    or ``async_entries`` lists) before attempting to match the provided unique ID.
    """

    if not isinstance(unique_id, str):
        return None

    candidates: list[Any] = []
    seen: set[int] = set()

    def _add(entry: Any) -> None:
        if entry is None:
            return
        identifier = id(entry)
        if identifier in seen:
            return
        seen.add(identifier)
        candidates.append(entry)

    def _extend(container: Any) -> None:
        if container is None:
            return
        if isinstance(container, dict):
            for value in container.values():
                _add(value)
            return
        if isinstance(container, (list, tuple, set, frozenset)):
            for value in container:
                _add(value)
            return
        _add(container)

    lookup = getattr(manager, "async_entries", None)
    if callable(lookup):
        try:
            _extend(lookup(domain))
        except TypeError:
            _extend(lookup())  # type: ignore[misc]

    for attribute in ("_entry", "entry", "entries", "_entries", "stored_entries"):
        _extend(getattr(manager, attribute, None))

    for entry in candidates:
        entry_domain = getattr(entry, "domain", None)
        if entry_domain is not None and entry_domain != domain:
            continue

        entry_unique_id = getattr(entry, "unique_id", None)
        if entry_unique_id == unique_id:
            return entry

        data = getattr(entry, "data", None)
        if isinstance(data, MutableMapping) and data.get("unique_id") == unique_id:
            return entry

    return None


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

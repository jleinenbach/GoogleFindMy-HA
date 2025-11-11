# tests/helpers/config_flow.py
"""Config flow helpers mirroring Home Assistant's manager contracts.

The utilities are shared across Google Find My tests and now include
:class:`ConfigEntriesFlowManagerStub`, a lightweight replacement for Home
Assistant's config-entry flow manager. The stub records each invocation of
``flow.async_init`` and exposes the recorded progress snapshots via
:meth:`async_progress` and ``async_progress_by_handler`` so tests can assert on
discovery abort reasons without reimplementing Home Assistant's bookkeeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
import asyncio
import importlib
import inspect
from types import SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import AsyncMock

from homeassistant.helpers import frame

__all__ = [
    "ConfigEntriesDomainUniqueIdLookupMixin",
    "ConfigEntriesFlowManagerStub",
    "config_entries_flow_stub",
    "prepare_flow_hass_config_entries",
    "set_config_flow_unique_id",
    "stub_async_entry_for_domain_unique_id",
]

FlowInitResult = dict[str, Any]
FlowInitCallable = Callable[..., Awaitable[FlowInitResult] | FlowInitResult]
_ConfigEntriesManagerT = TypeVar("_ConfigEntriesManagerT")


def _collect_manager_entries(manager: Any, domain: str | None) -> list[Any]:
    """Return entries and subentries tracked by ``manager``."""

    from .homeassistant import resolve_config_entry_lookup

    seen: set[int] = set()
    queue: list[Any] = []

    def _add(entry: Any) -> None:
        if entry is None:
            return
        identifier = id(entry)
        if identifier in seen:
            return
        seen.add(identifier)
        queue.append(entry)

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
        if isinstance(container, Iterable) and not isinstance(container, (str, bytes, bytearray)):
            for value in container:
                _add(value)
            return
        _add(container)

    lookup_domain = domain
    if lookup_domain is None:
        lookup_domain = getattr(manager, "domain", None)
        if lookup_domain is None:
            lookup_domain = getattr(getattr(manager, "flow", None), "domain", None)
        if lookup_domain is None:
            try:
                config_flow = importlib.import_module(
                    "custom_components.googlefindmy.config_flow"
                )
            except ModuleNotFoundError:
                config_flow = None
            lookup_domain = getattr(config_flow, "DOMAIN", None)

    lookup = getattr(manager, "async_entries", None)
    if callable(lookup):
        try:
            if lookup_domain is None:
                _extend(lookup())  # type: ignore[misc]
            else:
                _extend(lookup(lookup_domain))
        except TypeError:
            _extend(lookup())  # type: ignore[misc]

    for attribute in ("_entry", "entry", "entries", "_entries", "stored_entries"):
        _extend(getattr(manager, attribute, None))

    index = 0
    async_get_subentries = getattr(manager, "async_get_subentries", None)
    while index < len(queue):
        entry = queue[index]
        index += 1

        entry_id = getattr(entry, "entry_id", None)
        if callable(async_get_subentries) and isinstance(entry_id, str):
            try:
                _extend(async_get_subentries(entry_id))
            except TypeError:
                _extend(async_get_subentries())  # type: ignore[misc]

        runtime_data = getattr(entry, "runtime_data", None)
        manager_obj = getattr(runtime_data, "subentry_manager", None)
        managed = getattr(manager_obj, "managed_subentries", None)
        if isinstance(managed, dict):
            for key, value in managed.items():
                _add(value)
                resolved = resolve_config_entry_lookup(managed.values(), key)
                if resolved is not None:
                    _add(resolved)

        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            for key, value in subentries.items():
                _add(value)
                resolved = resolve_config_entry_lookup(subentries.values(), key)
                if resolved is not None:
                    _add(resolved)

        if isinstance(entry_id, str):
            resolved_entry = resolve_config_entry_lookup(queue, entry_id)
            if resolved_entry is not None:
                _add(resolved_entry)

    return queue


def _resolve_frame_module(frame_module: Any | None) -> Any:
    """Return the active Home Assistant frame helper module."""

    if frame_module is not None:
        return frame_module

    try:
        return importlib.import_module("homeassistant.helpers.frame")
    except ModuleNotFoundError:
        return frame


def _ensure_frame_setup(frame_module: Any, hass: Any) -> None:
    """Initialize the Home Assistant frame helper for the provided hass."""

    setup = getattr(frame_module, "set_up", None)
    if callable(setup):
        setup(hass)
        return

    async_setup = getattr(frame_module, "async_setup", None)
    if callable(async_setup):
        async_setup(hass)
        return

    async_set_up = getattr(frame_module, "async_set_up", None)
    if callable(async_set_up):
        result = async_set_up(hass)
        if asyncio.iscoroutine(result):
            asyncio.get_event_loop().run_until_complete(result)
        return


def prepare_flow_hass_config_entries(
    hass: Any,
    manager_factory: Callable[[], _ConfigEntriesManagerT],
    *,
    frame_module: Any | None = None,
) -> _ConfigEntriesManagerT:
    """Initialize Home Assistant flow stubs with a frame-aware manager."""

    active_frame = _resolve_frame_module(frame_module)
    _ensure_frame_setup(active_frame, hass)
    manager = manager_factory()
    hass.config_entries = manager
    return manager


def config_entries_flow_stub(
    *,
    result: FlowInitResult | FlowInitCallable | None = None,
) -> ConfigEntriesFlowManagerStub:
    """Return a config-entry manager stub exposing ``flow.async_init``.

    The helper now instantiates :class:`ConfigEntriesFlowManagerStub`, which
    records invocation order alongside Home Assistant-style progress snapshots.
    The returned stub exposes ``flow``, ``async_init``, ``calls``, and the
    :meth:`ConfigEntriesFlowManagerStub.async_progress` helpers so existing
    callers continue to work while tests migrate to the richer API.
    """

    return ConfigEntriesFlowManagerStub(result=result)


class ConfigEntriesFlowManagerStub:
    """Track ``flow.async_init`` calls with Home Assistant style progress."""

    _FLOW_ID_PREFIX = "flow"

    def __init__(
        self,
        *,
        result: FlowInitResult | FlowInitCallable | None = None,
    ) -> None:
        self._result = result
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._progress: list[dict[str, Any]] = []
        self._flow_counter = 0
        self.flow = SimpleNamespace(
            async_init=AsyncMock(side_effect=self._async_init),
            async_progress=self.async_progress,
            async_progress_by_handler=self.async_progress_by_handler,
        )

    @property
    def async_init(self) -> AsyncMock:
        """Return the ``flow.async_init`` coroutine for compatibility."""

        return self.flow.async_init

    def async_progress(self) -> list[dict[str, Any]]:
        """Return the recorded flow progress snapshots."""

        return [dict(record) for record in self._progress]

    def async_progress_by_handler(
        self,
        handler: Any,
        *,
        include_uninitialized: bool = False,
        match_context: MutableMapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return progress entries filtered by ``handler``."""

        matched: list[dict[str, Any]] = []
        for record in self._progress:
            if record.get("handler") != handler:
                continue
            if not include_uninitialized and record.get("step_id") is None:
                continue
            if match_context is not None:
                context = record.get("context")
                if not isinstance(context, MutableMapping):
                    continue
                if any(context.get(key) != value for key, value in match_context.items()):
                    continue
            matched.append(dict(record))
        return matched

    async def _async_init(self, *args: Any, **kwargs: Any) -> FlowInitResult:
        self.calls.append((args, dict(kwargs)))

        handler = kwargs.get("handler")
        if args:
            handler = args[0]

        context = kwargs.get("context")
        if isinstance(context, MutableMapping):
            context_snapshot: Any = dict(context)
        elif context is None:
            context_snapshot = {}
        else:
            context_snapshot = context

        self._flow_counter += 1
        flow_id = f"{self._FLOW_ID_PREFIX}_{self._flow_counter}"
        progress = {
            "flow_id": flow_id,
            "handler": handler,
            "context": context_snapshot,
            "step_id": None,
        }
        self._progress.append(progress)

        resolved: FlowInitResult | FlowInitCallable | None = self._result
        if callable(resolved):
            candidate = resolved(*args, **kwargs)
            if inspect.isawaitable(candidate):
                candidate = await candidate
            result: FlowInitResult = candidate
        elif resolved is not None:
            result = resolved
        else:
            result = {"type": "form", "step_id": None, "errors": {}}

        if isinstance(result, MutableMapping):
            progress["step_id"] = result.get("step_id")

        return result


def stub_async_entry_for_domain_unique_id(
    manager: Any, domain: str, unique_id: str
) -> Any | None:
    """Return the stored config entry matching ``domain``/``unique_id``.

    The Home Assistant core exposes :meth:`ConfigEntries.async_entry_for_domain_unique_id`
    to locate entries that already claimed a specific unique ID. The config-flow tests
    ship lightweight manager stubs instead of the production implementation, so this
    helper collects the available entries via :func:`resolve_config_entry_lookup` before
    attempting to match the provided unique ID across the common storage layouts used
    by the suite.
    """

    if not isinstance(unique_id, str):
        return None

    for entry in _collect_manager_entries(manager, domain):
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


class ConfigEntriesDomainUniqueIdLookupMixin:
    """Provide ``async_entry_for_domain_unique_id`` for config entry stubs."""

    # See ``tests/AGENTS.md`` ("Config entries unique ID lookup helper") for
    # guidance on reusing this mixin across new stubs instead of duplicating the
    # lookup wiring.
    
    def async_entry_for_domain_unique_id(
        self, domain: str, unique_id: str
    ) -> Any | None:
        return stub_async_entry_for_domain_unique_id(self, domain, unique_id)


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

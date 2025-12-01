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

import asyncio
import importlib
import inspect
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from types import ModuleType, SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import AsyncMock

__all__ = [
    "ConfigEntriesDomainUniqueIdLookupMixin",
    "ConfigEntriesFlowManagerStub",
    "attach_config_entries_flow_manager",
    "config_entries_flow_stub",
    "prepare_flow_hass_config_entries",
    "set_config_flow_unique_id",
    "stub_async_entry_for_domain_unique_id",
]

FlowInitResult = dict[str, Any]
FlowInitCallable = Callable[..., Awaitable[FlowInitResult] | FlowInitResult]
_ConfigEntriesManagerT = TypeVar("_ConfigEntriesManagerT")


def _collect_manager_entries(manager: Any, domain: str) -> list[Any]:
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

    lookup = getattr(manager, "async_entries", None)
    if callable(lookup):
        try:
            _extend(lookup(domain))
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


def _resolve_frame_module() -> Any:
    """Return the stubbed Home Assistant frame module."""

    module = sys.modules.get("homeassistant.helpers.frame")
    if module is not None:
        return module
    return importlib.import_module("homeassistant.helpers.frame")


def _configure_frame_helper(module: Any, hass: Any) -> None:
    """Attach the provided hass instance to the frame helper module."""

    helpers_pkg = sys.modules.setdefault(
        "homeassistant.helpers", ModuleType("homeassistant.helpers")
    )

    hass_holder = getattr(module, "_tests_hass_holder", None)
    if hass_holder is None:
        hass_holder = SimpleNamespace(hass=getattr(module, "hass", None))
        setattr(module, "_tests_hass_holder", hass_holder)

    class _HassProxy:
        __slots__ = ("_holder",)

        def __init__(self, holder: SimpleNamespace) -> None:
            self._holder = holder

        @property
        def hass(self) -> Any | None:  # pragma: no cover - simple proxy access
            return self._holder.hass

        @hass.setter
        def hass(self, value: Any | None) -> None:  # pragma: no cover - simple proxy access
            self._holder.hass = value

    hass_container = getattr(module, "_hass", None)
    if not isinstance(hass_container, _HassProxy):
        hass_container = _HassProxy(hass_holder)
        setattr(module, "_hass", hass_container)

    def _set_hass(target: Any | None) -> None:
        hass_holder.hass = target
        setattr(module, "hass", target)
        container = getattr(module, "_hass", None)
        if container is None or not hasattr(container, "hass"):
            container = SimpleNamespace(hass=target)
            setattr(module, "_hass", container)
        else:
            try:
                setattr(container, "hass", target)
            except AttributeError:
                container.hass = target

    setattr(module, "_tests_set_hass", _set_hass)

    if not getattr(module, "_tests_frame_stubbed", False):

        class _FrameHelper:
            """Minimal frame helper shim compatible with Home Assistant tests."""

            def __init__(self) -> None:
                self._is_setup = False
                self.hass: Any | None = None

            def set_up(self, hass: Any | None) -> None:
                self._is_setup = True
                if hass is not None:
                    self.hass = hass

            async def async_set_up(self, hass: Any | None) -> None:
                self.set_up(hass)

            def report(self, *args: Any, **kwargs: Any) -> None:
                return None

            def report_usage(self, *args: Any, **kwargs: Any) -> None:
                return None

            def __getattr__(self, name: str) -> Any:
                if name.startswith("async_set_up") or name.startswith("async_setup"):
                    async def _async_proxy(hass: Any | None) -> None:
                        result = self.async_set_up(hass)
                        if inspect.isawaitable(result):
                            await result

                    return _async_proxy

                if name.startswith("set_up") and name != "set_up":
                    def _setup_proxy(hass: Any | None) -> None:
                        self.set_up(hass)

                    return _setup_proxy

                raise AttributeError(name)

        frame_helper = _FrameHelper()

        def _report_usage_proxy(*args: Any, **kwargs: Any) -> None:
            stored_hass = hass_holder.hass
            if stored_hass is not None:
                _set_hass(stored_hass)
            frame_helper.report_usage(*args, **kwargs)

        configured = getattr(module, "_configured_instances", None)
        if not isinstance(configured, list):
            configured = []
            setattr(module, "_configured_instances", configured)

        original_set_up = getattr(module, "set_up", None)
        original_async_setup = getattr(module, "async_setup", None)

        def _set_up(target: Any) -> None:
            configured.append(target)
            _set_hass(target)
            frame_helper.hass = target
            frame_helper.set_up(target)
            if callable(original_set_up) and original_set_up is not _set_up:
                original_set_up(target)

        async def _async_set_up(target: Any) -> None:
            _set_up(target)
            if callable(original_async_setup) and original_async_setup is not _async_set_up:
                result = original_async_setup(target)
                if inspect.isawaitable(result):
                    await result

        module.set_up = _set_up  # type: ignore[assignment]
        module.setup = _set_up  # type: ignore[assignment]
        module.async_set_up = _async_set_up  # type: ignore[assignment]
        module.async_setup = _async_set_up  # type: ignore[assignment]
        module.report = frame_helper.report  # type: ignore[assignment]
        module.report_usage = _report_usage_proxy  # type: ignore[assignment]
        module.frame_helper = frame_helper  # type: ignore[assignment]
        setattr(module, "_tests_report_usage_proxy", _report_usage_proxy)
        setattr(module, "_tests_frame_stubbed", True)

    setup = getattr(module, "set_up", None) or getattr(module, "setup", None)
    if callable(setup):
        setup(hass)

    async_setup = getattr(module, "async_set_up", None) or getattr(
        module, "async_setup", None
    )
    if callable(async_setup):
        result = async_setup(hass)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
            else:
                loop.create_task(result)

    sys.modules["homeassistant.helpers.frame"] = module
    setattr(helpers_pkg, "frame", module)
    _set_hass(hass)

    def _apply_config_entry_patches() -> None:
        try:
            config_entries_module = importlib.import_module(
                "homeassistant.config_entries"
            )
        except ModuleNotFoundError:
            return

        def _noop_report_usage(*args: Any, **kwargs: Any) -> None:
            return None

        config_entries_module.report_usage = _noop_report_usage  # type: ignore[assignment]
        config_entries_module.__dict__["report_usage"] = _noop_report_usage

        options_flow_cls = getattr(config_entries_module, "OptionsFlow", None)
        options_flow_with_reload = getattr(
            config_entries_module, "OptionsFlowWithReload", None
        )

        def _assign_config_entry(target: Any, value: Any) -> None:
            hass_obj = getattr(target, "hass", None)
            if hass_obj is not None:
                set_hass = getattr(module, "_tests_set_hass", None)
                if callable(set_hass):
                    set_hass(hass_obj)
                else:
                    container = getattr(module, "_hass", None)
                    if hasattr(container, "hass"):
                        try:
                            setattr(container, "hass", hass_obj)
                        except AttributeError:
                            container.hass = hass_obj
                    else:
                        setattr(module, "_hass", SimpleNamespace(hass=hass_obj))
                    setattr(module, "hass", hass_obj)
            setattr(target, "_config_entry", value)

        def _wrap_options_flow_property(cls: Any) -> None:
            if cls is None:
                return
            current_prop = getattr(cls, "config_entry", None)
            if isinstance(current_prop, property):
                original_set_config_entry = current_prop.fset

                def _set_config_entry(self: Any, value: Any) -> None:
                    try:
                        if callable(original_set_config_entry):
                            original_set_config_entry(self, value)
                            return
                    except RuntimeError as err:
                        if str(err) != "Frame helper not set up":
                            raise
                    _assign_config_entry(self, value)

                patched = property(
                    current_prop.fget,
                    _set_config_entry,
                    current_prop.fdel,
                    current_prop.__doc__,
                )
            else:

                def _get_config_entry(self: Any) -> Any:
                    return getattr(self, "_config_entry", None)

                def _set_config_entry(self: Any, value: Any) -> None:
                    _assign_config_entry(self, value)

                patched = property(_get_config_entry, _set_config_entry, None, None)

            cls.config_entry = patched  # type: ignore[assignment]

        _wrap_options_flow_property(options_flow_cls)
        _wrap_options_flow_property(options_flow_with_reload)

        try:
            integration_flow_module = importlib.import_module(
                "custom_components.googlefindmy.config_flow"
            )
        except ModuleNotFoundError:
            integration_flow_module = None
        if integration_flow_module is not None:
            handler_cls = getattr(
                integration_flow_module, "OptionsFlowHandler", None
            )
            if handler_cls is not None:

                class _ConfigEntryDescriptor:
                    def __get__(self, instance: Any, owner: Any) -> Any:
                        if instance is None:
                            return self
                        return getattr(instance, "_config_entry", None)

                    def __set__(self, instance: Any, value: Any) -> None:
                        _assign_config_entry(instance, value)

                handler_cls.config_entry = _ConfigEntryDescriptor()  # type: ignore[assignment]

    _apply_config_entry_patches()

    if not getattr(module, "_tests_import_reload_hook", False):
        original_reload = importlib.reload

        def _patched_reload(target: ModuleType, *args: Any, **kwargs: Any) -> ModuleType:
            result = original_reload(target, *args, **kwargs)
            if target.__name__ in {
                "homeassistant.config_entries",
                "custom_components.googlefindmy.config_flow",
            }:
                _apply_config_entry_patches()
            return result

        importlib.reload = _patched_reload  # type: ignore[assignment]
        setattr(module, "_tests_import_reload_hook", True)
        setattr(module, "_tests_original_reload", original_reload)


def _ensure_flow_handler_default() -> None:
    """Ensure ConfigFlow instances expose the integration domain as handler."""

    try:
        flow_module = importlib.import_module(
            "custom_components.googlefindmy.config_flow"
        )
    except ModuleNotFoundError:
        return

    flow_cls = getattr(flow_module, "ConfigFlow", None)
    if flow_cls is None or getattr(flow_cls, "_tests_handler_init_patched", False):
        return

    original_init = flow_cls.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if getattr(self, "handler", None) is None:
            domain = getattr(flow_cls, "domain", None)
            if isinstance(domain, str):
                setattr(self, "handler", domain)

    flow_cls.__init__ = _patched_init  # type: ignore[assignment]
    setattr(flow_cls, "_tests_handler_init_patched", True)


def _patch_service_validation_error() -> None:
    """Replace Home Assistant's validation error with a test-friendly variant."""

    try:
        exceptions_module = importlib.import_module("homeassistant.exceptions")
    except ModuleNotFoundError:
        return

    cls = getattr(exceptions_module, "ServiceValidationError", None)
    if cls is None or getattr(cls, "_tests_str_patched", False):
        return

    original_init = cls.__init__

    def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        message = kwargs.pop("message", None)
        original_init(self, *args, **kwargs)
        if message is not None:
            setattr(self, "_tests_message", message)

    def _patched_str(self: Any) -> str:
        cached = getattr(self, "_tests_message", None)
        if isinstance(cached, str):
            return cached
        translation_domain = getattr(self, "translation_domain", None)
        translation_key = getattr(self, "translation_key", None)
        placeholders = getattr(self, "translation_placeholders", None)
        if translation_domain and translation_key:
            if isinstance(placeholders, Mapping):
                placeholder_str = ", ".join(
                    f"{key}={value}" for key, value in sorted(placeholders.items())
                )
                return f"{translation_domain}:{translation_key} ({placeholder_str})"
            return f"{translation_domain}:{translation_key}"
        return "Service validation error"

    cls.__init__ = _patched_init  # type: ignore[assignment]
    cls.__str__ = _patched_str  # type: ignore[assignment]
    setattr(cls, "_tests_str_patched", True)


def prepare_flow_hass_config_entries(
    hass: Any,
    manager_factory: Callable[[], _ConfigEntriesManagerT],
    *,
    frame_module: Any | None = None,
) -> _ConfigEntriesManagerT:
    """Initialize Home Assistant flow stubs with a frame-aware manager."""

    module = _resolve_frame_module() if frame_module is None else frame_module
    _configure_frame_helper(module, hass)
    _ensure_flow_handler_default()
    _patch_service_validation_error()
    if not hasattr(hass, "loop_thread_id"):
        hass.loop_thread_id = threading.get_ident()
    if not hasattr(hass, "loop"):
        try:
            hass.loop = asyncio.get_running_loop()
        except RuntimeError:
            hass.loop = asyncio.new_event_loop()
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


def attach_config_entries_flow_manager(
    target: Any,
    *,
    result: FlowInitResult | FlowInitCallable | None = None,
) -> ConfigEntriesFlowManagerStub:
    """Attach a :class:`ConfigEntriesFlowManagerStub` to ``target``.

    The helper stores the flow manager on ``target.flow_manager`` and exposes its
    ``flow`` namespace alongside the ``async_progress`` helpers directly on the
    target for convenience.
    """

    manager = ConfigEntriesFlowManagerStub(result=result)
    target.flow_manager = manager
    target.flow = manager.flow
    target.async_progress = manager.async_progress  # type: ignore[attr-defined]
    target.async_progress_by_handler = manager.async_progress_by_handler  # type: ignore[attr-defined]
    return manager


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
        match_context: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return progress entries filtered by ``handler``."""

        del include_uninitialized

        matches: list[dict[str, Any]] = []
        for record in self._progress:
            if handler is not None and record.get("handler") != handler:
                continue
            if match_context:
                context = record.get("context")
                if not isinstance(context, Mapping):
                    context = {}
                if any(
                    context.get(key) != value
                    for key, value in match_context.items()
                ):
                    continue
            matches.append(dict(record))
        return matches

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

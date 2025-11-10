# tests/conftest.py
"""Test configuration and environment stubs for integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
import asyncio
import importlib
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from types import MappingProxyType, ModuleType, SimpleNamespace
from datetime import datetime, timezone
import json

import pytest


class _FakeIssueRegistry:
    """Lightweight repair issue registry used across tests."""

    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}

    def async_get_issue(self, domain: str, issue_id: str) -> dict[str, Any] | None:
        issue = self.issues.get(issue_id)
        if issue and issue.get("domain") == domain:
            return issue
        return None


@dataclass(slots=True)
class IssueRegistryCapture:
    """Record repair issue interactions while stubbing the registry."""

    created: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    registry: _FakeIssueRegistry = field(default_factory=_FakeIssueRegistry)


@pytest.fixture
def hass_executor_stub() -> Callable[..., SimpleNamespace]:
    """Return a factory that yields hass stubs with executor support."""

    async def _async_add_executor_job(
        func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _factory(**attrs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            async_add_executor_job=_async_add_executor_job,
            **attrs,
        )

    return _factory


@pytest.fixture
def issue_registry_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> IssueRegistryCapture:
    """Provide a patched issue registry and capture create/delete calls."""

    capture = IssueRegistryCapture()

    def _create_issue(
        hass: Any,
        domain: str,
        issue_id: str,
        *,
        translation_placeholders: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        payload = {
            "issue_id": issue_id,
            "domain": domain,
            "translation_placeholders": dict(translation_placeholders or {}),
        }
        capture.created.append(payload)
        capture.registry.issues[issue_id] = payload

    def _delete_issue(hass: Any, domain: str, issue_id: str) -> None:
        capture.deleted.append((domain, issue_id))
        capture.registry.issues.pop(issue_id, None)

    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_get", lambda hass: capture.registry
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_create_issue", _create_issue
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.ir.async_delete_issue", _delete_issue
    )

    return capture


@pytest.fixture
def subentry_support(monkeypatch: pytest.MonkeyPatch):
    """Toggle config subentry support between modern and legacy cores."""

    from custom_components.googlefindmy import config_flow

    original_flow = getattr(config_flow, "ConfigSubentryFlow", None)
    original_subentry = getattr(config_flow, "ConfigSubentry", None)
    fallback_flow = getattr(config_flow, "_FALLBACK_CONFIG_SUBENTRY_FLOW", None)

    class _SubentrySupportToggle:
        """Helper exposing modern and legacy subentry configurations."""

        def as_modern(self) -> object | None:
            """Restore the module to the original subentry-capable state."""

            monkeypatch.setattr(
                config_flow,
                "ConfigSubentryFlow",
                original_flow,
                raising=False,
            )
            monkeypatch.setattr(
                config_flow,
                "_FALLBACK_CONFIG_SUBENTRY_FLOW",
                fallback_flow,
                raising=False,
            )
            monkeypatch.setattr(
                config_flow,
                "ConfigSubentry",
                original_subentry,
                raising=False,
            )
            return original_flow

        def as_legacy(self) -> type[object]:
            """Simulate a core that lacks ConfigSubentry support."""

            legacy_flow = fallback_flow
            if legacy_flow is None:
                legacy_flow = type(
                    "_LegacyFallbackSubentryFlow",
                    (),
                    {"__doc__": "Fallback stub for legacy config subentries."},
                )

            monkeypatch.setattr(
                config_flow,
                "ConfigSubentryFlow",
                legacy_flow,
                raising=False,
            )
            monkeypatch.setattr(
                config_flow,
                "_FALLBACK_CONFIG_SUBENTRY_FLOW",
                legacy_flow,
                raising=False,
            )
            monkeypatch.setattr(
                config_flow,
                "ConfigSubentry",
                None,
                raising=False,
            )
            return legacy_flow

    toggle = _SubentrySupportToggle()
    toggle.as_modern()
    return toggle

# Ensure the package root is importable without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INTEGRATION_ROOT = ROOT / "custom_components" / "googlefindmy"

SERVICE_SUBENTRY_KEY: str = "service"
TRACKER_SUBENTRY_KEY: str = "tracker"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Accept the asyncio mode ini option when pytest-asyncio is absent."""

    if importlib.util.find_spec("pytest_asyncio") is not None:
        return

    try:
        parser.addini(
            "asyncio_mode",
            "Default asyncio mode placeholder when pytest-asyncio is unavailable.",
        )
    except ValueError:
        # Another plugin already registered the option; reuse it.
        return


def pytest_configure(config: pytest.Config) -> None:
    """Register the asyncio marker for coroutine-based tests."""

    config.addinivalue_line(
        "markers",
        "asyncio: execute the coroutine test using an isolated event loop",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    """Execute asyncio-marked coroutine tests without requiring pytest-asyncio."""

    marker = pyfuncitem.get_closest_marker("asyncio")
    if marker is None or not asyncio.iscoroutinefunction(pyfuncitem.obj):
        return None

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        argnames = getattr(pyfuncitem._fixtureinfo, "argnames", ())  # noqa: SLF001 - pytest internals
        if any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in inspect.signature(pyfuncitem.obj).parameters.values()
        ):
            call_kwargs = pyfuncitem.funcargs
        else:
            call_kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in argnames
                if name in pyfuncitem.funcargs
            }
        loop.run_until_complete(pyfuncitem.obj(**call_kwargs))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return True


def _stub_homeassistant() -> None:
    """Install lightweight stubs for Home Assistant modules required at import time."""

    ha_pkg = sys.modules.setdefault("homeassistant", ModuleType("homeassistant"))
    ha_pkg.__path__ = getattr(ha_pkg, "__path__", [])  # mark as package

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.SOURCE_DISCOVERY = "discovery"
    config_entries.SOURCE_RECONFIGURE = "reconfigure"

    subentry_counter = {"value": 0}

    class _UndefinedType:
        """Sentinel mirroring Home Assistant's UNDEFINED."""

        def __repr__(self) -> str:  # pragma: no cover - debugging helper
            return "UNDEFINED"

    UNDEFINED = _UndefinedType()

    def _next_subentry_id() -> str:
        subentry_counter["value"] += 1
        return f"subentry-{subentry_counter['value']}"

    class ConfigEntry:  # minimal placeholder
        pass

    class ConfigEntryState:  # minimal enum-like placeholder
        LOADED = "loaded"
        NOT_LOADED = "not_loaded"
        SETUP_ERROR = "setup_error"
        SETUP_RETRY = "setup_retry"
        SETUP_IN_PROGRESS = "setup_in_progress"
        MIGRATION_ERROR = "migration_error"

    class ConfigEntryAuthFailed(Exception):
        pass

    class ConfigFlow:
        """Minimal stub matching the ConfigFlow API used in tests."""

        VERSION = 1

        def __init_subclass__(cls, **kwargs):  # type: ignore[override]
            domain = kwargs.pop("domain", None)
            super().__init_subclass__()

            if kwargs:
                for key, value in kwargs.items():
                    setattr(cls, key, value)

            if domain is not None:
                setattr(cls, "domain", domain)

            handlers = getattr(config_entries, "HANDLERS", None)
            if isinstance(handlers, dict):
                registry_domain = getattr(cls, "domain", None)
                if isinstance(registry_domain, str):
                    handlers[registry_domain] = cls

        def __init__(self) -> None:
            self.context: dict[str, object] = {}
            self.hass = None

        async def async_set_unique_id(
            self, unique_id: str, *, raise_on_progress: bool = False
        ) -> None:
            self.unique_id = unique_id  # type: ignore[attr-defined]
            self._unique_id = unique_id  # type: ignore[attr-defined]

        async def async_show_form(
            self, *args, **kwargs
        ):  # pragma: no cover - defensive
            return {"type": "form"}

        async def async_show_menu(
            self, *args, **kwargs
        ):  # pragma: no cover - defensive
            return {"type": "menu"}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_create_entry(
            self, *, title: str, data: Mapping[str, Any], options: Mapping[str, Any] | None = None
        ) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "type": "create_entry",
                "title": title,
                "data": dict(data),
            }
            if options is not None:
                entry["options"] = dict(options)
            return entry

        def _set_confirm_only(self) -> None:
            self.context["confirm_only"] = True

        def _async_current_entries(self, *, include_ignore: bool = False):
            hass = getattr(self, "hass", None)
            if hass is None:
                return []
            manager = getattr(hass, "config_entries", None)
            if manager is None:
                return []
            try:
                return list(manager.async_entries(getattr(self, "handler", None)))
            except Exception:  # noqa: BLE001 - best effort fallback
                return []

        def _abort_if_unique_id_configured(
            self,
            *,
            updates=None,
            reload: bool = True,
            **_: object,
        ) -> None:
            current_entries = self._async_current_entries()
            target_unique_id = getattr(self, "unique_id", None)
            if not current_entries or target_unique_id is None:
                return

            for entry in current_entries:
                if getattr(entry, "unique_id", None) != target_unique_id:
                    continue

                if updates:
                    update_callable = getattr(
                        getattr(self.hass, "config_entries", None),
                        "async_update_entry",
                        None,
                    )
                    if callable(update_callable):
                        update_callable(entry, **updates)

                    if reload:
                        reload_callable = getattr(
                            getattr(self.hass, "config_entries", None),
                            "async_reload",
                            None,
                        )
                        if callable(reload_callable):
                            outcome = reload_callable(entry.entry_id)
                            if inspect.isawaitable(outcome):
                                try:
                                    loop = asyncio.get_running_loop()
                                except RuntimeError:  # pragma: no cover - fallback path
                                    asyncio.run(outcome)
                                else:
                                    loop.create_task(outcome)
                return

        def add_suggested_values_to_schema(self, schema, suggested):  # noqa: D401 - stub
            return schema

    class OptionsFlow:
        """Minimal OptionsFlow stub for imports."""

        def async_show_form(self, *args, **kwargs):  # pragma: no cover - defensive
            return {"type": "form"}

        def async_create_entry(
            self, *, title: str, data
        ):  # pragma: no cover - defensive
            return {"type": "create_entry", "title": title, "data": data}

        def async_abort(self, **kwargs):  # pragma: no cover - defensive
            return {"type": "abort", **kwargs}

        def add_suggested_values_to_schema(self, schema, suggested):  # noqa: D401 - stub
            return schema

    class OptionsFlowWithReload(OptionsFlow):
        """Placeholder inheriting OptionsFlow behaviour."""

    class ConfigSubentry:
        """Simple ConfigSubentry stand-in used by unit tests."""

        def __init__(
            self,
            *,
            data: Mapping[str, object] | MappingProxyType | dict[str, object],
            subentry_type: str,
            title: str,
            unique_id: str | None = None,
            subentry_id: str | None = None,
            translation_key: str | None = None,
        ) -> None:
            self.data: Mapping[str, object] = MappingProxyType(dict(data))
            self.subentry_type: str = subentry_type
            self.title: str = title
            self.unique_id: str | None = unique_id
            self.subentry_id: str = subentry_id or _next_subentry_id()
            self.translation_key: str | None = translation_key
            self.entry_id: str = self.subentry_id

        def as_dict(self) -> dict[str, object]:  # pragma: no cover - helper parity
            return {
                "data": dict(self.data),
                "subentry_id": self.subentry_id,
                "subentry_type": self.subentry_type,
                "title": self.title,
                "unique_id": self.unique_id,
                "translation_key": self.translation_key,
            }

    class ConfigSubentryFlow:
        """Lightweight ConfigSubentryFlow stub mirroring HA attributes.

        Home Assistant instantiates config-subentry flow handlers with the
        signature ``ConfigSubentryFlow(config_entry, config_subentry)``. Future
        tests should therefore pass the parent ``ConfigEntry`` alongside either
        an existing ``ConfigSubentry`` or a freshly constructed instance when
        spinning up a handler. The stub stores both objects verbatim so the
        tests can assert against ``config_entry`` state, the ``subentry`` data,
        or the generated ``subentry_id`` without recreating the wiring logic in
        every test.
        """

        def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry) -> None:
            self.config_entry = entry
            self.subentry = subentry
            self.subentry_id = subentry.subentry_id
            self.subentry_type = subentry.subentry_type
            self.data: Mapping[str, object] = subentry.data
            self.title = subentry.title
            self.unique_id = subentry.unique_id
            self.translation_key = subentry.translation_key

        async def async_step_init(
            self, user_input: Mapping[str, object] | None = None
        ) -> dict[str, object]:
            """Record the provided data and mimic a simple form response."""

            self._last_step = ("init", MappingProxyType(dict(user_input or {})))
            return {"type": "form", "step_id": "init"}

        async def async_update_and_abort(
            self,
            *,
            data: Mapping[str, object],
            reason: str,
        ) -> dict[str, object]:
            """Persist updates and return an abort result like HA."""

            self.data = MappingProxyType(dict(data))
            return {"type": "abort", "reason": reason, "data": dict(data)}

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    config_entries.HANDLERS = {}
    config_entries.ConfigSubentry = ConfigSubentry
    config_entries.ConfigSubentryFlow = ConfigSubentryFlow
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.OptionsFlowWithReload = OptionsFlowWithReload
    config_entries.UNDEFINED = UNDEFINED
    sys.modules["homeassistant.config_entries"] = config_entries

    try:
        import voluptuous as vol_module  # type: ignore[import]
    except ImportError:
        vol_module = ModuleType("voluptuous")

        class _Schema:
            def __init__(self, schema, *args, **kwargs):
                self.schema = schema

            def __call__(self, value):  # pragma: no cover - defensive
                return value

        class _Marker:
            def __init__(self, key):
                self.key = key
                self.schema = {key}

            def __hash__(self) -> int:  # pragma: no cover - defensive
                return hash(self.key)

            def __eq__(self, other: object) -> bool:  # pragma: no cover - defensive
                if isinstance(other, _Marker):
                    return self.key == other.key
                return self.key == other

        class _Invalid(Exception):
            pass

        class _MultipleInvalid(_Invalid):
            pass

        def _identity(value):
            return value

        vol_module.Schema = _Schema  # type: ignore[attr-defined]
        vol_module.Invalid = _Invalid  # type: ignore[attr-defined]
        vol_module.MultipleInvalid = _MultipleInvalid  # type: ignore[attr-defined]
        vol_module.ALLOW_EXTRA = object()  # type: ignore[attr-defined]
        vol_module.PREVENT_EXTRA = object()  # type: ignore[attr-defined]

        def _optional(key, default=None, **_: object) -> _Marker:
            return _Marker(key)

        def _required(key, description=None, default=None, **_: object) -> _Marker:
            return _Marker(key)

        vol_module.Optional = _optional  # type: ignore[attr-defined]
        vol_module.Required = _required  # type: ignore[attr-defined]
        vol_module.Any = lambda *items, **kwargs: _identity  # type: ignore[attr-defined]
        vol_module.All = lambda *validators, **kwargs: _identity  # type: ignore[attr-defined]
        vol_module.In = lambda items: _identity  # type: ignore[attr-defined]
        vol_module.Range = lambda **kwargs: _identity  # type: ignore[attr-defined]
        vol_module.Coerce = lambda typ: _identity  # type: ignore[attr-defined]
        vol_module.Match = lambda pattern, msg=None: _identity  # type: ignore[attr-defined]
        vol_module.ExactSequence = lambda seq: _identity  # type: ignore[attr-defined]
        vol_module.Clamp = lambda **kwargs: _identity  # type: ignore[attr-defined]
        sys.modules["voluptuous"] = vol_module
    else:
        sys.modules["voluptuous"] = vol_module

    data_entry_flow = ModuleType("homeassistant.data_entry_flow")

    class _FlowResultType:
        """Enum-like container mirroring Home Assistant's flow result types."""

        ABORT = "abort"
        CREATE_ENTRY = "create_entry"
        FORM = "form"
        MENU = "menu"

    class _AbortFlow(Exception):
        """Stub AbortFlow matching Home Assistant's interface."""

        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    data_entry_flow.AbortFlow = _AbortFlow  # type: ignore[attr-defined]
    data_entry_flow.FlowResultType = _FlowResultType  # type: ignore[attr-defined]
    data_entry_flow.FlowResult = dict  # type: ignore[assignment]
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow

    const_module = ModuleType("homeassistant.const")

    class Platform:  # enum-like stub covering platforms used in __init__
        DEVICE_TRACKER = "device_tracker"
        BUTTON = "button"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    const_module.EVENT_HOMEASSISTANT_STARTED = "start"
    const_module.EVENT_HOMEASSISTANT_STOP = "stop"
    const_module.ATTR_LATITUDE = "latitude"
    const_module.ATTR_LONGITUDE = "longitude"
    const_module.ATTR_GPS_ACCURACY = "gps_accuracy"
    const_module.STATE_UNAVAILABLE = "unavailable"
    const_module.STATE_UNKNOWN = "unknown"
    const_module.Platform = Platform
    sys.modules["homeassistant.const"] = const_module

    loader_module = ModuleType("homeassistant.loader")

    async def _async_get_integration(_hass, _domain):  # pragma: no cover - stub
        return SimpleNamespace(name="stub", version="0.0.0")

    loader_module.async_get_integration = _async_get_integration
    sys.modules["homeassistant.loader"] = loader_module
    setattr(ha_pkg, "loader", loader_module)

    core_module = ModuleType("homeassistant.core")

    class CoreState:  # minimal CoreState stub
        running = "running"

    class ServiceCall:  # pragma: no cover - stub for service handlers
        def __init__(self, data=None):
            self.data = data or {}

    class Event(SimpleNamespace):
        """Minimal Event stub carrying type and data payload."""

        def __init__(self, event_type: str, data: Mapping[str, Any] | None = None) -> None:
            super().__init__(event_type=event_type, data=data or {})

    class HomeAssistant:  # minimal HomeAssistant placeholder
        state = CoreState.running

    core_module.CoreState = CoreState
    core_module.HomeAssistant = HomeAssistant
    core_module.ServiceCall = ServiceCall
    core_module.Event = Event
    core_module.callback = lambda func: func
    sys.modules["homeassistant.core"] = core_module

    exceptions_module = ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    class ServiceValidationError(HomeAssistantError):
        """Stubbed ServiceValidationError carrying translation context."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args)
            self.translation_domain = kwargs.get("translation_domain")
            self.translation_key = kwargs.get("translation_key")
            self.translation_placeholders = kwargs.get("translation_placeholders")

    exceptions_module.HomeAssistantError = HomeAssistantError
    exceptions_module.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions_module.ServiceValidationError = ServiceValidationError
    exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules["homeassistant.exceptions"] = exceptions_module

    helpers_pkg = sys.modules.setdefault(
        "homeassistant.helpers", ModuleType("homeassistant.helpers")
    )
    helpers_pkg.__path__ = getattr(helpers_pkg, "__path__", [])

    for sub in (
        "device_registry",
        "entity_registry",
        "issue_registry",
        "update_coordinator",
    ):
        module_name = f"homeassistant.helpers.{sub}"
        module = ModuleType(module_name)
        sys.modules[module_name] = module
        setattr(helpers_pkg, sub, module)

    entity_module = ModuleType("homeassistant.helpers.entity")

    class DeviceInfo:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    entity_module.DeviceInfo = DeviceInfo

    def split_entity_id(entity_id: str) -> tuple[str, str]:
        if "." not in entity_id:
            raise ValueError(entity_id)
        domain, object_id = entity_id.split(".", 1)
        return domain, object_id

    entity_module.split_entity_id = split_entity_id

    class EntityCategory:
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    entity_module.EntityCategory = EntityCategory
    sys.modules["homeassistant.helpers.entity"] = entity_module
    setattr(helpers_pkg, "entity", entity_module)

    entity_component_module = ModuleType(
        "homeassistant.helpers.entity_component"
    )
    entity_component_module.split_entity_id = split_entity_id
    sys.modules["homeassistant.helpers.entity_component"] = entity_component_module
    setattr(helpers_pkg, "entity_component", entity_component_module)

    from collections.abc import Callable, Iterable

    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform_module.AddEntitiesCallback = Callable[[Iterable], None]
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform_module
    setattr(helpers_pkg, "entity_platform", entity_platform_module)

    restore_state_module = ModuleType("homeassistant.helpers.restore_state")

    class RestoreEntity:
        async def async_get_last_state(self):  # pragma: no cover - stub behaviour
            return None

    restore_state_module.RestoreEntity = RestoreEntity
    sys.modules["homeassistant.helpers.restore_state"] = restore_state_module
    setattr(helpers_pkg, "restore_state", restore_state_module)

    issue_registry_module = sys.modules["homeassistant.helpers.issue_registry"]
    issue_registry_module.IssueSeverity = SimpleNamespace(ERROR="error", INFO="info")

    class _IssueRegistry:
        """Minimal in-memory Repairs issue registry used by tests."""

        def __init__(self) -> None:
            self._issues: dict[tuple[str, str], dict[str, object]] = {}

        def async_get_issue(
            self, domain: str, issue_id: str
        ) -> dict[str, object] | None:
            return self._issues.get((domain, issue_id))

        def async_create_issue(
            self,
            domain: str,
            issue_id: str,
            **data: object,
        ) -> None:
            self._issues[(domain, issue_id)] = {
                **data,
                "domain": domain,
                "issue_id": issue_id,
            }

        def async_delete_issue(self, domain: str, issue_id: str) -> None:
            self._issues.pop((domain, issue_id), None)

    def _issue_registry_for(hass) -> _IssueRegistry:
        registry = getattr(hass, "_issue_registry", None)
        if registry is None:
            registry = _IssueRegistry()
            setattr(hass, "_issue_registry", registry)
        return registry

    issue_registry_module.async_get = _issue_registry_for

    def _async_create_issue(hass, domain, issue_id, **data) -> None:
        _issue_registry_for(hass).async_create_issue(domain, issue_id, **data)

    def _async_delete_issue(hass, domain, issue_id) -> None:
        _issue_registry_for(hass).async_delete_issue(domain, issue_id)

    issue_registry_module.async_create_issue = _async_create_issue
    issue_registry_module.async_delete_issue = _async_delete_issue

    device_registry_module = sys.modules["homeassistant.helpers.device_registry"]
    device_registry_module.EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"

    if not hasattr(device_registry_module, "DeviceEntryType"):
        class DeviceEntryType:  # noqa: D401 - stub enum container
            SERVICE = "service"

        device_registry_module.DeviceEntryType = DeviceEntryType

    class _StubDeviceEntry:
        """In-memory device entry capturing registry metadata."""

        _counter = 0

        def __init__(
            self,
            *,
            identifiers: set[tuple[str, str]],
            config_entry_id: str,
            name: str | None = None,
            manufacturer: str | None = None,
            model: str | None = None,
            sw_version: str | None = None,
            entry_type: object | None = None,
            configuration_url: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: Mapping[str, str] | None = None,
            config_subentry_id: str | None = None,
            via_device_id: str | None = None,
            via_device: tuple[str, str] | None = None,
        ) -> None:
            type(self)._counter += 1
            self.id = f"device-{type(self)._counter}"
            self.identifiers = set(identifiers)
            self.config_entries = {config_entry_id}
            self.name = name
            self.name_by_user = None
            self.manufacturer = manufacturer
            self.model = model
            self.sw_version = sw_version
            self.entry_type = entry_type
            self.configuration_url = configuration_url
            self.translation_key = translation_key
            self.translation_placeholders = dict(translation_placeholders or {})
            self.config_subentry_id = config_subentry_id
            self.via_device_id = via_device_id
            self.via_device = via_device
            self.disabled_by = None

        def update(self, **changes: object) -> None:
            for key, value in changes.items():
                setattr(self, key, value)

    _MISSING = object()

    class _StubDeviceRegistry:
        """Minimal registry implementation retaining created/updated metadata."""

        def __init__(self) -> None:
            self.devices: dict[str, _StubDeviceEntry] = {}
            self.created: list[dict[str, object]] = []
            self.updated: list[dict[str, object]] = []

        def async_get(self, device_id: str | None) -> _StubDeviceEntry | None:
            if not device_id:
                return None
            return self.devices.get(device_id)

        def async_get_device(
            self, *, identifiers: set[tuple[str, str]] | None = None
        ) -> _StubDeviceEntry | None:
            if not identifiers:
                return None
            for device in self.devices.values():
                if identifiers & device.identifiers:
                    return device
            return None

        def async_get_or_create(
            self,
            *,
            config_entry_id: str,
            identifiers: set[tuple[str, str]],
            manufacturer: str,
            model: str,
            name: str | None = None,
            via_device_id: str | None = None,
            via_device: tuple[str, str] | None = None,
            sw_version: str | None = None,
            entry_type: object | None = None,
            configuration_url: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: Mapping[str, str] | None = None,
            config_subentry_id: str | None = None,
        ) -> _StubDeviceEntry:
            entry = _StubDeviceEntry(
                identifiers=identifiers,
                config_entry_id=config_entry_id,
                name=name,
                manufacturer=manufacturer,
                model=model,
                sw_version=sw_version,
                entry_type=entry_type,
                configuration_url=configuration_url,
                translation_key=translation_key,
                translation_placeholders=translation_placeholders,
                config_subentry_id=config_subentry_id,
                via_device_id=via_device_id,
                via_device=via_device,
            )
            self.devices[entry.id] = entry
            self.created.append(
                {
                    "config_entry_id": config_entry_id,
                    "identifiers": set(identifiers),
                    "manufacturer": manufacturer,
                    "model": model,
                    "name": name,
                    "via_device_id": via_device_id,
                    "via_device": via_device,
                    "sw_version": sw_version,
                    "entry_type": entry_type,
                    "configuration_url": configuration_url,
                    "translation_key": translation_key,
                    "translation_placeholders": dict(translation_placeholders or {}),
                    "config_subentry_id": config_subentry_id,
                }
            )
            return entry

        def async_update_device(
            self,
            *,
            device_id: str,
            new_identifiers: set[tuple[str, str]] | None = None,
            via_device_id: str | None = None,
            translation_key: str | None = None,
            translation_placeholders: Mapping[str, str] | None = None,
            config_subentry_id: object = _MISSING,
            add_config_subentry_id: object = _MISSING,
            config_entry_id: object = _MISSING,
            add_config_entry_id: object = _MISSING,
            name: str | None = None,
            manufacturer: str | None = None,
            model: str | None = None,
            sw_version: str | None = None,
            entry_type: object | None = None,
            configuration_url: str | None = None,
        ) -> None:
            device = self.devices.get(device_id)
            if device is None:
                raise AssertionError(f"Unknown device_id {device_id}")
            if new_identifiers is not None:
                device.identifiers = set(new_identifiers)
            updates: dict[str, object | None] = {}
            if config_entry_id is not _MISSING:
                if config_entry_id is None:
                    device.config_entries = set()
                else:
                    device.config_entries = {cast(str, config_entry_id)}
                updates["config_entry_id"] = cast(str | None, config_entry_id)
            if add_config_entry_id is not _MISSING:
                if add_config_entry_id is None:
                    device.config_entries = set()
                else:
                    entries = set(
                        getattr(device, "config_entries", set()) or set()
                    )
                    entries.add(cast(str, add_config_entry_id))
                    device.config_entries = entries
                updates["add_config_entry_id"] = cast(
                    str | None, add_config_entry_id
                )
            effective_subentry = _MISSING
            if config_subentry_id is not _MISSING:
                effective_subentry = config_subentry_id
            elif add_config_subentry_id is not _MISSING:
                effective_subentry = add_config_subentry_id
            if effective_subentry is not _MISSING:
                device.config_subentry_id = cast(str | None, effective_subentry)
                updates["config_subentry_id"] = cast(str | None, effective_subentry)
            for attr, value in (
                ("via_device_id", via_device_id),
                ("translation_key", translation_key),
                ("config_subentry_id", config_subentry_id),
                ("name", name),
                ("manufacturer", manufacturer),
                ("model", model),
                ("sw_version", sw_version),
                ("entry_type", entry_type),
                ("configuration_url", configuration_url),
            ):
                if attr == "config_subentry_id":
                    continue
                if value is not None:
                    updates[attr] = value
            if translation_placeholders is not None:
                updates["translation_placeholders"] = dict(translation_placeholders)
            if updates:
                device.update(**updates)
            self.updated.append(
                {
                    "device_id": device_id,
                    "new_identifiers": None
                    if new_identifiers is None
                    else set(new_identifiers),
                    "via_device_id": via_device_id,
                    "translation_key": translation_key,
                    "translation_placeholders": None
                    if translation_placeholders is None
                    else dict(translation_placeholders),
                    "config_entry_id": None
                    if config_entry_id is _MISSING
                    else cast(str | None, config_entry_id),
                    "config_subentry_id": None
                    if effective_subentry is _MISSING
                    else cast(str | None, effective_subentry),
                    "name": name,
                    "manufacturer": manufacturer,
                    "model": model,
                    "sw_version": sw_version,
                    "entry_type": entry_type,
                    "configuration_url": configuration_url,
                    "add_config_subentry_id": None
                    if add_config_subentry_id is _MISSING
                    else cast(str | None, add_config_subentry_id),
                    "add_config_entry_id": None
                    if add_config_entry_id is _MISSING
                    else cast(str | None, add_config_entry_id),
                }
            )

    def _device_registry_for(hass=None) -> _StubDeviceRegistry:
        if hass is not None:
            registry = getattr(hass, "_device_registry_stub", None)
            if registry is None:
                registry = _StubDeviceRegistry()
                setattr(hass, "_device_registry_stub", registry)
            return registry
        return _StubDeviceRegistry()

    device_registry_module.async_get = _device_registry_for

    cv_module = ModuleType("homeassistant.helpers.config_validation")

    def _multi_select(choices):  # pragma: no cover - defensive
        return lambda value: value

    cv_module.multi_select = _multi_select
    sys.modules["homeassistant.helpers.config_validation"] = cv_module
    setattr(helpers_pkg, "config_validation", cv_module)

    aiohttp_client_module = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client_module.async_get_clientsession = lambda hass: None
    async def _stub_async_make_resolver(*_args, **_kwargs):
        return None

    aiohttp_client_module._async_make_resolver = _stub_async_make_resolver
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_module
    setattr(helpers_pkg, "aiohttp_client", aiohttp_client_module)

    storage_module = ModuleType("homeassistant.helpers.storage")

    class Store:  # minimal async Store stub
        def __init__(self, *args, **kwargs) -> None:
            self._data: dict[str, object] | None = None

        async def async_load(self) -> dict[str, object] | None:
            return self._data

        def async_delay_save(self, *_args, **_kwargs) -> None:
            return None

        async def async_remove(self) -> None:
            self._data = None

    storage_module.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage_module
    setattr(helpers_pkg, "storage", storage_module)

    class UpdateFailed(Exception):
        pass

    update_coordinator_module = sys.modules["homeassistant.helpers.update_coordinator"]
    update_coordinator_module.UpdateFailed = UpdateFailed

    from typing import Generic, TypeVar

    _T = TypeVar("_T")

    class DataUpdateCoordinator(Generic[_T]):
        """Minimal stub for DataUpdateCoordinator supporting subclassing."""

        def __init__(
            self, hass=None, logger=None, name: str | None = None, update_interval=None
        ):
            self.hass = hass
            self.logger = logger
            self.name = name or "coordinator"
            self.update_interval = update_interval

        async def async_request_refresh(
            self,
        ) -> None:  # pragma: no cover - stubbed behaviour
            return None

        async def async_config_entry_first_refresh(
            self,
        ) -> None:  # pragma: no cover - stubbed behaviour
            return None

    update_coordinator_module.DataUpdateCoordinator = DataUpdateCoordinator

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:  # pragma: no cover - stub behaviour
            return None

        async def async_added_to_hass(self) -> None:  # pragma: no cover - stub behaviour
            return None

        def __class_getitem__(cls, _item):  # pragma: no cover - typing compatibility
            return cls

        @property
        def unique_id(self) -> str | None:
            return getattr(self, "_attr_unique_id", None)

    update_coordinator_module.CoordinatorEntity = CoordinatorEntity

    event_module = ModuleType("homeassistant.helpers.event")

    async def _async_call_later(
        *_args, **_kwargs
    ):  # pragma: no cover - stubbed behaviour
        return None

    event_module.async_call_later = _async_call_later
    sys.modules["homeassistant.helpers.event"] = event_module
    setattr(helpers_pkg, "event", event_module)

    network_module = ModuleType("homeassistant.helpers.network")
    network_module.get_url = lambda *args, **kwargs: "https://example.local"
    sys.modules["homeassistant.helpers.network"] = network_module
    setattr(helpers_pkg, "network", network_module)

    entity_registry_module = sys.modules["homeassistant.helpers.entity_registry"]

    class _StubEntityRegistryEntry:
        """Entity registry entry capturing subentry assignments for assertions."""

        __slots__ = (
            "entity_id",
            "platform",
            "unique_id",
            "config_entry_id",
            "config_entry_subentry_id",
            "device_id",
        )

        def __init__(
            self,
            *,
            entity_id: str,
            platform: str,
            unique_id: str | None,
            config_entry_id: str | None,
            config_entry_subentry_id: str | None,
        ) -> None:
            self.entity_id = entity_id
            self.platform = platform
            self.unique_id = unique_id
            self.config_entry_id = config_entry_id
            self.config_entry_subentry_id = config_entry_subentry_id
            self.device_id: str | None = None

    class _StubEntityRegistry:
        """In-memory entity registry stub used by platform setup tests."""

        def __init__(self) -> None:
            self.entities: dict[str, _StubEntityRegistryEntry] = {}

        def async_get(self, entity_id: str) -> _StubEntityRegistryEntry | None:
            return self.entities.get(entity_id)

        def async_get_entity_id(self, platform: str, unique_id: str) -> str | None:
            for entity_id, entry in self.entities.items():
                if entry.platform == platform and entry.unique_id == unique_id:
                    return entity_id
            return None

        def record_entity(
            self,
            entity_id: str,
            *,
            platform: str,
            unique_id: str | None,
            config_entry_id: str | None,
            config_entry_subentry_id: str | None,
        ) -> _StubEntityRegistryEntry:
            entry = _StubEntityRegistryEntry(
                entity_id=entity_id,
                platform=platform,
                unique_id=unique_id,
                config_entry_id=config_entry_id,
                config_entry_subentry_id=config_entry_subentry_id,
            )
            self.entities[entity_id] = entry
            return entry

    def _entity_registry_for(
        hass: Any | None = None,
    ) -> _StubEntityRegistry:  # pragma: no cover - stub behaviour
        if hass is not None:
            registry = getattr(hass, "_entity_registry_stub", None)
            if registry is None:
                registry = _StubEntityRegistry()
                setattr(hass, "_entity_registry_stub", registry)
            return registry
        return _StubEntityRegistry()

    entity_registry_module.RegistryEntry = _StubEntityRegistryEntry
    entity_registry_module.async_get = _entity_registry_for

    util_pkg = sys.modules.setdefault(
        "homeassistant.util", ModuleType("homeassistant.util")
    )
    dt_module = ModuleType("homeassistant.util.dt")
    dt_module.UTC = timezone.utc
    dt_module.utcnow = lambda: datetime.now(timezone.utc)
    dt_module.now = dt_module.utcnow
    dt_module.as_local = lambda dt: dt
    sys.modules["homeassistant.util.dt"] = dt_module
    setattr(util_pkg, "dt", dt_module)

    components_pkg = sys.modules.setdefault(
        "homeassistant.components", ModuleType("homeassistant.components")
    )
    components_pkg.__path__ = getattr(components_pkg, "__path__", [])

    device_tracker_module = ModuleType("homeassistant.components.device_tracker")

    class SourceType:
        GPS = "gps"

    class TrackerEntity:  # pragma: no cover - stub behaviour
        _attr_has_entity_name = True

        def __init__(self, *_args, **_kwargs) -> None:
            pass

    device_tracker_module.DOMAIN = "device_tracker"
    device_tracker_module.SourceType = SourceType
    device_tracker_module.TrackerEntity = TrackerEntity
    sys.modules["homeassistant.components.device_tracker"] = device_tracker_module
    setattr(components_pkg, "device_tracker", device_tracker_module)

    def _entity_base() -> type:
        class _EntityBase:  # pragma: no cover - stub behaviour
            _attr_has_entity_name = True

            def __init__(self, *_args, **_kwargs) -> None:
                self.entity_id = None
                self.hass = None

            async def async_added_to_hass(self) -> None:
                return None

            async def async_will_remove_from_hass(self) -> None:
                return None

            def async_write_ha_state(self) -> None:
                return None

            @property
            def unique_id(self) -> str | None:
                return getattr(self, "_attr_unique_id", None)

        return _EntityBase

    button_module = ModuleType("homeassistant.components.button")

    class ButtonEntity(_entity_base()):  # pragma: no cover - stub
        pass

    class ButtonEntityDescription:  # pragma: no cover - stub
        def __init__(self, **kwargs) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    button_module.ButtonEntity = ButtonEntity
    button_module.ButtonEntityDescription = ButtonEntityDescription
    sys.modules["homeassistant.components.button"] = button_module
    setattr(components_pkg, "button", button_module)

    binary_sensor_module = ModuleType("homeassistant.components.binary_sensor")

    class BinarySensorEntity(_entity_base()):  # pragma: no cover - stub
        pass

    class BinarySensorEntityDescription:  # pragma: no cover - stub
        def __init__(self, **kwargs) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class BinarySensorDeviceClass:  # pragma: no cover - stub values
        PROBLEM = "problem"

    binary_sensor_module.BinarySensorEntity = BinarySensorEntity
    binary_sensor_module.BinarySensorEntityDescription = BinarySensorEntityDescription
    binary_sensor_module.BinarySensorDeviceClass = BinarySensorDeviceClass
    sys.modules["homeassistant.components.binary_sensor"] = binary_sensor_module
    setattr(components_pkg, "binary_sensor", binary_sensor_module)

    sensor_module = ModuleType("homeassistant.components.sensor")

    class SensorEntity(_entity_base()):  # pragma: no cover - stub
        pass

    class RestoreSensor(SensorEntity):  # pragma: no cover - stub
        async def async_get_last_sensor_data(self):
            return None

    class SensorEntityDescription:  # pragma: no cover - stub
        def __init__(self, **kwargs) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class SensorDeviceClass:  # pragma: no cover - stub values
        TIMESTAMP = "timestamp"

    class SensorStateClass:  # pragma: no cover - stub values
        TOTAL_INCREASING = "total_increasing"

    sensor_module.SensorEntity = SensorEntity
    sensor_module.RestoreSensor = RestoreSensor
    sensor_module.SensorEntityDescription = SensorEntityDescription
    sensor_module.SensorDeviceClass = SensorDeviceClass
    sensor_module.SensorStateClass = SensorStateClass
    sys.modules["homeassistant.components.sensor"] = sensor_module
    setattr(components_pkg, "sensor", sensor_module)

    http_module = ModuleType("homeassistant.components.http")

    class HomeAssistantView:  # pragma: no cover - stub for imports
        requires_auth = False

        async def get(self, *_args, **_kwargs):
            return None

    http_module.HomeAssistantView = HomeAssistantView
    sys.modules["homeassistant.components.http"] = http_module
    setattr(components_pkg, "http", http_module)

    diagnostics_module = ModuleType("homeassistant.components.diagnostics")

    def _async_redact_data(data, _keys):  # pragma: no cover - stub behaviour
        return data

    diagnostics_module.async_redact_data = _async_redact_data
    sys.modules["homeassistant.components.diagnostics"] = diagnostics_module
    setattr(components_pkg, "diagnostics", diagnostics_module)

    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.get_instance = lambda *args, **kwargs: None
    history_module = ModuleType("homeassistant.components.recorder.history")

    def _no_history(*args, **kwargs):  # pragma: no cover - stub
        raise NotImplementedError

    history_module.get_significant_states = _no_history
    recorder_module.history = history_module
    sys.modules["homeassistant.components.recorder"] = recorder_module
    sys.modules["homeassistant.components.recorder.history"] = history_module
    setattr(components_pkg, "recorder", recorder_module)


@pytest.fixture(name="record_flow_forms")
def fixture_record_flow_forms() -> Callable[[Any], list[str | None]]:
    """Instrument a config flow to record the step IDs shown to the user."""

    def _apply(flow: Any) -> list[str | None]:
        recorded: list[str | None] = []

        async def _show_form(*_: Any, **kwargs: Any) -> dict[str, Any]:
            step_id = kwargs.get("step_id")
            recorded.append(step_id)
            response: dict[str, Any] = {"type": "form"}
            if step_id is not None:
                response["step_id"] = step_id
            return response

        flow.async_show_form = _show_form  # type: ignore[attr-defined]
        return recorded

    return _apply


@pytest.fixture(scope="session", name="integration_root")
def fixture_integration_root() -> Path:
    """Return the root path of the googlefindmy integration package."""

    assert INTEGRATION_ROOT.is_dir(), "integration package root must exist"
    return INTEGRATION_ROOT


@pytest.fixture(scope="session", name="integration_python_files")
def fixture_integration_python_files(integration_root: Path) -> list[Path]:
    """Return all Python files under the integration root (sorted)."""

    return sorted(integration_root.rglob("*.py"))


@pytest.fixture(scope="session", name="manifest")
def fixture_manifest(integration_root: Path) -> dict[str, object]:
    """Load and return the integration manifest."""

    manifest_path = integration_root / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest_data, dict)
    return manifest_data


@pytest.fixture(name="stub_coordinator_factory")
def fixture_stub_coordinator_factory() -> Callable[..., type[Any]]:
    """Return a factory that builds coordinator stubs with async helpers."""

    def _factory(
        *,
        data: Iterable[dict[str, Any]] | None = None,
        stats: Mapping[str, Any] | None = None,
        performance_metrics: Mapping[str, Any] | None = None,
        last_update_success: bool = True,
        subentry_key: str = TRACKER_SUBENTRY_KEY,
        service_subentry_key: str = SERVICE_SUBENTRY_KEY,
        metadata_for_feature: Mapping[str, str] | None = None,
        snapshot_callback: Callable[[str | None, str | None], Iterable[dict[str, Any]]] | None = None,
        init_hook: Callable[..., None] | None = None,
        methods: Mapping[str, Callable[..., Any]] | None = None,
        extra_attributes: Mapping[str, Any] | None = None,
    ) -> type[Any]:
        base_data = list(data) if data is not None else [{"id": "device-1", "name": "Device"}]
        base_stats = dict(stats or {"background_updates": 1})
        base_performance = dict(performance_metrics or {})
        feature_map = dict(metadata_for_feature or {})

        def _coerce_attribute(value: Any) -> Any:
            return value() if callable(value) else value

        class _CoordinatorStub:
            """Coordinator stub matching the runtime contract used in tests."""

            def __init__(self, hass: Any, *, cache: Any, **kwargs: Any) -> None:
                self.hass = hass
                self.cache = cache
                self.data = list(base_data)
                self.stats = dict(base_stats)
                self.performance_metrics = dict(base_performance)
                self.last_update_success = last_update_success
                self.config_entry: Any | None = None
                self._purged: list[str] = []
                self._listeners: list[Callable[[], None]] = []
                self._subentry_key = subentry_key
                self._service_subentry_key = service_subentry_key
                self._metadata_for_feature = dict(feature_map)
                self._snapshot_callback = snapshot_callback
                self._init_kwargs = dict(kwargs)
                self.subentry_manager: Any | None = None
                self._device_names: dict[str, str] = {}
                self._device_location_data: dict[str, Any] = {}
                self._device_caps: dict[str, Any] = {}
                self._present_last_seen: dict[str, float] = {}
                if extra_attributes:
                    for key, value in extra_attributes.items():
                        setattr(self, key, _coerce_attribute(value))
                if init_hook is not None:
                    init_hook(self, hass=hass, cache=cache, kwargs=kwargs)

            def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
                self._listeners.append(listener)
                return lambda: None

            def find_tracker_entity_entry(self, device_id: str) -> Any | None:
                del device_id
                return None

            def force_poll_due(self) -> None:  # pragma: no cover - default no-op
                return None

            async def async_setup(self) -> None:
                return None

            async def async_refresh(self) -> None:
                return None

            async def async_shutdown(self) -> None:
                return None

            async def async_request_refresh(self) -> None:  # pragma: no cover - optional
                return None

            def async_set_updated_data(self, _data: Any) -> None:  # pragma: no cover - optional
                return None

            def push_updated(self, _ids: list[str]) -> None:  # pragma: no cover - optional
                return None

            def purge_device(self, device_id: str) -> None:
                self._purged.append(device_id)

            def stable_subentry_identifier(
                self, *, key: str | None = None, feature: str | None = None
            ) -> str:
                if key is not None:
                    return key
                if feature is not None and feature in self._metadata_for_feature:
                    return self._metadata_for_feature[feature]
                if feature == "binary_sensor":
                    return self._service_subentry_key
                return self._subentry_key

            def get_subentry_metadata(
                self, *, key: str | None = None, feature: str | None = None
            ) -> Any:
                resolved = self.stable_subentry_identifier(key=key, feature=feature)
                return SimpleNamespace(key=resolved)

            def get_subentry_snapshot(
                self, key: str | None = None, *, feature: str | None = None
            ) -> list[dict[str, Any]]:
                if self._snapshot_callback is not None:
                    return list(self._snapshot_callback(key, feature))
                return list(self.data)

            def is_device_visible_in_subentry(
                self, subentry_key: str, device_id: str
            ) -> bool:
                return True

            def attach_subentry_manager(self, manager: Any) -> None:
                self.subentry_manager = manager

        if methods:
            for name, method in methods.items():
                setattr(_CoordinatorStub, name, method)

        return _CoordinatorStub

    return _factory
_stub_homeassistant()


def _load_integration_constant(attribute: str) -> str:
    """Resolve a constant from the integration module after stubs are ready."""

    const_module = importlib.import_module("custom_components.googlefindmy.const")
    return cast(str, getattr(const_module, attribute))


SERVICE_SUBENTRY_KEY = _load_integration_constant("SERVICE_SUBENTRY_KEY")
TRACKER_SUBENTRY_KEY = _load_integration_constant("TRACKER_SUBENTRY_KEY")

components_pkg = importlib.import_module("custom_components")
components_pkg.__path__ = [str(ROOT / "custom_components")]

gf_pkg = importlib.import_module("custom_components.googlefindmy")
setattr(components_pkg, "googlefindmy", gf_pkg)

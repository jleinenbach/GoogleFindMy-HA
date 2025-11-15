from __future__ import annotations

"""Home Assistant ``config_entries`` stub installer for the test suite.

Calling :func:`install_config_entries_stubs` attaches canonical stand-ins for
``ConfigEntry``/``ConfigFlow`` plus their related exception classes (for
example, ``ConfigEntryAuthFailed`` and ``OperationNotAllowed``) to the provided
module. Downstream shims import those attributes straight from the populated
module instead of re-declaring them, keeping the entire test tree aligned with
the consolidated helper documented in ``tests/AGENTS.md``.
"""

import asyncio
import inspect
from collections.abc import Mapping
from itertools import count
from types import MappingProxyType, ModuleType
from typing import Any

__all__ = ["install_config_entries_stubs"]


def install_config_entries_stubs(target: ModuleType) -> None:
    """Populate ``homeassistant.config_entries``-style stubs on ``target``."""

    subentry_counter = count(1)

    def _next_subentry_id() -> str:
        return f"subentry-{next(subentry_counter)}"

    class _UndefinedType:
        """Sentinel mirroring Home Assistant's ``UNDEFINED`` constant."""

        def __repr__(self) -> str:  # pragma: no cover - debugging helper
            return "UNDEFINED"

    class ConfigEntry:  # minimal placeholder
        """ConfigEntry stub used by tests before HA loads."""

        pass

    class ConfigEntryState:
        """Enum-like placeholder matching Home Assistant states."""

        LOADED = "loaded"
        NOT_LOADED = "not_loaded"
        SETUP_ERROR = "setup_error"
        SETUP_RETRY = "setup_retry"
        SETUP_IN_PROGRESS = "setup_in_progress"
        MIGRATION_ERROR = "migration_error"

    class ConfigEntryAuthFailed(Exception):
        """Exception mirroring Home Assistant's auth failure error."""

        pass

    class OperationNotAllowed(Exception):
        """Stub mirroring Home Assistant's OperationNotAllowed error."""

        def __init__(self, message: str = "") -> None:
            super().__init__(message)

    handlers: dict[str, type[object]] = {}

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

    target.ConfigEntry = ConfigEntry
    target.ConfigEntryState = ConfigEntryState
    target.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    target.OperationNotAllowed = OperationNotAllowed
    target.ConfigSubentry = ConfigSubentry
    target.ConfigSubentryFlow = ConfigSubentryFlow
    target.ConfigFlow = ConfigFlow
    target.OptionsFlow = OptionsFlow
    target.OptionsFlowWithReload = OptionsFlowWithReload
    target.UNDEFINED = _UndefinedType()
    target.HANDLERS = handlers

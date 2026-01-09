"""Device registry operations for GoogleFindMyCoordinator.

This module contains registry-related methods extracted from main.py
during Phase 2 of the refactoring.

Methods moved here:
- _call_device_registry_api: Core registry call with compatibility handling
- _device_registry_kwargs_need_legacy_retry: Legacy kwarg detection
- _device_registry_build_legacy_kwargs: Legacy kwarg translation
- _device_registry_config_subentry_kwarg_name: Subentry kwarg detection
- _device_registry_allows_translation_update: Translation support check
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import (
    UnknownEntry,
    UnknownSubEntry,
)

from .helpers.registry import (
    build_legacy_device_registry_kwargs as _build_legacy_kwargs_impl,
    needs_legacy_kwarg_retry as _needs_legacy_retry_impl,
)

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


class RegistryOperations:
    """Device registry operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage device registry entries,
    including creation, updates, and synchronization of HA device registry
    with the Google Find My device list.
    """

    def _call_device_registry_api(
        self: "GoogleFindMyCoordinator",
        call: Callable[..., Any],
        *,
        base_kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a device registry API, handling keyword compatibility."""

        kwargs = dict(base_kwargs or {})
        if "config_subentry_id" in kwargs:
            replacement = self._device_registry_config_subentry_kwarg_name(call)
            if replacement is None:
                kwargs.pop("config_subentry_id")
            elif replacement != "config_subentry_id":
                kwargs[replacement] = kwargs.pop("config_subentry_id")

        try:
            return call(**kwargs)
        except TypeError as err:
            if not self._device_registry_kwargs_need_legacy_retry(call, err, kwargs):
                raise

            legacy_kwargs = self._device_registry_build_legacy_kwargs(kwargs)
            _LOGGER.debug(
                "Retrying device registry call %s with legacy keyword arguments after %s",
                getattr(call, "__qualname__", repr(call)),
                err,
            )
            return call(**legacy_kwargs)
        except (UnknownEntry, UnknownSubEntry) as err:
            kwarg_name = self._device_registry_config_subentry_kwarg_name(call)
            if (
                kwarg_name == "add_config_subentry_id"
                or "config_subentry_id" not in kwargs
            ):
                raise
            _LOGGER.debug(
                "Device registry call %s rejected config_subentry_id (%s); retrying without it",
                getattr(call, "__qualname__", repr(call)),
                err,
            )
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("config_subentry_id", None)
            return call(**fallback_kwargs)

    def _device_registry_kwargs_need_legacy_retry(
        self: "GoogleFindMyCoordinator",
        call: Callable[..., Any],
        err: TypeError,
        kwargs: Mapping[str, Any],
    ) -> bool:
        """Return True when ``kwargs`` must be rewritten for legacy cores."""
        kwarg_name = self._device_registry_config_subentry_kwarg_name(call)
        return _needs_legacy_retry_impl(kwarg_name, str(err), kwargs)

    @staticmethod
    def _device_registry_build_legacy_kwargs(
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Translate modern device-registry kwargs to their legacy names."""
        return _build_legacy_kwargs_impl(kwargs)

    def _device_registry_config_subentry_kwarg_name(
        self: "GoogleFindMyCoordinator", call: Callable[..., Any]
    ) -> str | None:
        """Return the config-subentry kwarg name accepted by ``call``.

        Home Assistant 2025.11 renamed the ``async_update_device`` keyword from
        ``config_subentry_id`` to ``add_config_subentry_id``. Earlier versions still
        expect ``config_subentry_id``. This helper inspects the callable signature
        and returns the supported keyword, caching the result for reuse.
        """

        cache_attr = "_device_registry_config_subentry_kwarg_cache"
        cache_obj = getattr(self, cache_attr, None)
        cache: dict[Callable[..., Any], str | None]
        if isinstance(cache_obj, dict):
            cache = cast(dict[Callable[..., Any], str | None], cache_obj)
        else:
            cache = cast(dict[Callable[..., Any], str | None], {})
            setattr(self, cache_attr, cache)

        func = getattr(call, "__func__", call)
        if func in cache:
            return cache[func]

        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):  # pragma: no cover - defensive fallback
            kwarg_name: str | None = None
        else:
            parameters = signature.parameters
            if "config_subentry_id" in parameters:
                kwarg_name = "config_subentry_id"
            elif "add_config_subentry_id" in parameters:
                kwarg_name = "add_config_subentry_id"
            elif any(
                param.kind is inspect.Parameter.VAR_KEYWORD
                for param in parameters.values()
            ):
                kwarg_name = "config_subentry_id"
            else:
                kwarg_name = None

        cache[func] = kwarg_name
        return kwarg_name

    def _device_registry_allows_translation_update(
        self: "GoogleFindMyCoordinator", dev_reg: Any
    ) -> bool:
        """Return True if the registry accepts translation metadata during updates."""

        cached = getattr(self, "_device_registry_supports_translation_update", None)
        if isinstance(cached, bool):
            return cached

        update_helper = getattr(dev_reg, "async_update_device", None)
        supports_translation = False
        if callable(update_helper):
            try:
                signature = inspect.signature(update_helper)
            except (TypeError, ValueError):
                supports_translation = False
            else:
                params = signature.parameters
                supports_translation = (
                    "translation_key" in params and "translation_placeholders" in params
                )

        setattr(
            self, "_device_registry_supports_translation_update", supports_translation
        )
        return supports_translation

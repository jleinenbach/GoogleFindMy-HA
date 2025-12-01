# custom_components/googlefindmy/util_services.py
"""Helpers for registering backward-compatible entity services."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

LOGGER = logging.getLogger(__name__)


def register_entity_service(
    entity_platform: Any,
    service: str,
    schema: Any,
    func: str | Callable[..., Any],
) -> None:
    """Register an entity service using the newest API available.

    Home Assistant 2024.12 introduced ``async_register_platform_entity_service`` to
    ensure entity services scoped to a platform coexist with helper registration
    order. Older releases only expose ``async_register_entity_service``. This helper
    bridges both versions so the integration can continue working across core
    releases without littering the codebase with compatibility branches.
    """

    try:
        register_platform = getattr(
            entity_platform, "async_register_platform_entity_service"
        )
    except AttributeError:
        register_platform = None

    if callable(register_platform):
        try:
            register_platform(service, schema, func)
            return
        except TypeError:
            # API mismatch (older HA backport); fall back to legacy variant below.
            pass

    try:
        entity_platform.async_register_entity_service(service, schema, func)
    except ValueError:
        LOGGER.debug(
            "Entity service %s already registered; skipping duplicate registration",
            service,
        )

"""Helpers to reuse the stub coordinator factory outside pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from tests.conftest import (
    fixture_coordinator_teardown_defaults,
    fixture_stub_coordinator_factory,
)

__all__ = ["build_stub_coordinator", "get_stub_coordinator_factory"]


def get_stub_coordinator_factory(**factory_kwargs: Any) -> Callable[..., type[Any]]:
    """Return the pytest stub coordinator factory without requiring fixtures."""

    factory = fixture_stub_coordinator_factory()
    if not factory_kwargs:
        return factory
    return lambda **kwargs: factory(**factory_kwargs, **kwargs)


def build_stub_coordinator(
    *,
    hass: Any | None = None,
    cache: Any = None,
    factory_kwargs: Mapping[str, Any] | None = None,
    instance_kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Instantiate a coordinator stub with teardown defaults for ad-hoc debugging."""

    factory = get_stub_coordinator_factory(**(factory_kwargs or {}))
    coordinator_cls = factory()
    coordinator = coordinator_cls(hass, cache=cache, **(instance_kwargs or {}))

    apply_defaults = fixture_coordinator_teardown_defaults()
    apply_defaults(coordinator)

    return coordinator

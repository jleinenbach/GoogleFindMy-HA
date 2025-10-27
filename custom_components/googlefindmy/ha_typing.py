# custom_components/googlefindmy/ha_typing.py
"""Typed shims for Home Assistant base classes lacking typing metadata."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast, Generic

from homeassistant.core import callback as ha_callback

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., Any])


def callback(func: _CallbackT) -> _CallbackT:
    """Return a typed wrapper around Home Assistant's ``callback`` decorator."""

    return cast(_CallbackT, ha_callback(func))


if TYPE_CHECKING:
    from aiohttp import web
    from homeassistant.core import HomeAssistant

    _CoordinatorT = TypeVar("_CoordinatorT")

    class HomeAssistantView:
        """Structural type for views served through the HTTP component."""

        url: str
        name: str
        requires_auth: bool
        hass: HomeAssistant

        async def get(
            self, request: web.Request, *args: Any, **kwargs: Any
        ) -> web.Response: ...

    class _EntityBase:
        """Common entity protocol for Home Assistant platform entities."""

        hass: HomeAssistant
        entity_id: str | None

        async def async_added_to_hass(self) -> None: ...

        async def async_will_remove_from_hass(self) -> None: ...

        def async_write_ha_state(self) -> None: ...

    class CoordinatorEntity(_EntityBase, Generic[_CoordinatorT]):
        """Structural type for coordinator-backed entities."""

        coordinator: _CoordinatorT

        def __init__(self, coordinator: _CoordinatorT) -> None: ...

    class ButtonEntity(_EntityBase):
        """Structural type for button platform entities."""

    class BinarySensorEntity(_EntityBase):
        """Structural type for binary_sensor platform entities."""

    class SensorEntity(_EntityBase):
        """Structural type for sensor platform entities."""

    class RestoreSensor(SensorEntity):
        """Structural type for restore-capable sensors."""

        async def async_get_last_sensor_data(self) -> Any: ...

    class TrackerEntity(_EntityBase):
        """Structural type for device_tracker entities."""

    class RestoreEntity(_EntityBase):
        """Structural type for restore-capable generic entities."""

        async def async_get_last_state(self) -> Any: ...

else:
    from homeassistant.components.button import ButtonEntity  # noqa: F401
    from homeassistant.components.binary_sensor import BinarySensorEntity  # noqa: F401
    from homeassistant.components.device_tracker import TrackerEntity  # noqa: F401
    from homeassistant.components.http import HomeAssistantView  # noqa: F401
    from homeassistant.components.sensor import (  # noqa: F401
        RestoreSensor,
        SensorEntity,
    )
    from homeassistant.helpers.restore_state import RestoreEntity  # noqa: F401
    from homeassistant.helpers.update_coordinator import CoordinatorEntity  # noqa: F401

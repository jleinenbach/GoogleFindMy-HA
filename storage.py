from __future__ import annotations

from typing import Dict, Any
from homeassistant.helpers.storage import Store

_KEY = "googlefindmy_last_known"
_VERSION = 1


class LastKnownStore:
    """Tiny JSON store for last known coordinates per device (HA .storage/)."""

    def __init__(self, hass) -> None:
        self._store = Store(hass, _VERSION, _KEY)

    async def async_load(self) -> Dict[str, Dict[str, Any]]:
        data = await self._store.async_load()
        return data or {}

    async def async_save(self, mapping: Dict[str, Dict[str, Any]]) -> None:
        await self._store.async_save(mapping)

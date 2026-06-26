# tests/helpers/registry_mixin_stub.py
"""Test stub for :class:`RegistryOperations` mixin standalone unit tests.

PR 1.A.1b (AP-1.1a-β): exercise mixin methods M1-M14 + M17 without the
full coordinator. ``_MixinBase`` is runtime-empty, so a minimal subclass
seeds every attribute the mixin reads, and cross-mixin methods owned by
``SubentryOperations``/``IdentityOperations`` are pre-mocked to dodge the
``NotImplementedError`` traps (risk R-NEU-3 in the notes sidecar).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from custom_components.googlefindmy.const import DOMAIN, TRACKER_SUBENTRY_KEY
from custom_components.googlefindmy.coordinator.registry import RegistryOperations


class _DevRegStub:
    """In-memory device registry; replaces ``dr.async_get(hass)`` results.

    Holds devices as ``SimpleNamespace`` so tests can mutate ``.identifiers``,
    ``.disabled_by`` etc. without touching real Home Assistant classes.
    """

    def __init__(self) -> None:
        self.devices: list[SimpleNamespace] = []
        self.update_calls: list[dict[str, Any]] = []
        self._next_id = 0

    def add_device(
        self,
        *,
        identifiers: set[tuple[str, str]] | None = None,
        name: str | None = None,
        name_by_user: str | None = None,
        disabled_by: Any | None = None,
        config_entries: set[str] | None = None,
    ) -> SimpleNamespace:
        """Insert a synthetic device entry and return it."""

        self._next_id += 1
        device = SimpleNamespace(
            id=f"dev-{self._next_id}",
            identifiers=set(identifiers or set()),
            name=name,
            name_by_user=name_by_user,
            disabled_by=disabled_by,
            config_entries=set(config_entries or set()),
        )
        self.devices.append(device)
        return device

    def async_update_device(
        self, device_id: str, **kwargs: Any
    ) -> SimpleNamespace | None:
        """Stub used by ``_device_registry_allows_translation_update`` (signature only)."""

        self.update_calls.append({"device_id": device_id, **kwargs})
        for device in self.devices:
            if device.id == device_id:
                for key, value in kwargs.items():
                    setattr(device, key, value)
                return device
        return None


class _EntRegStub:
    """In-memory entity registry; replaces ``er.async_get(hass)`` results."""

    def __init__(self) -> None:
        self.entries: list[SimpleNamespace] = []
        self._next_id = 0

    def add_entry(
        self,
        *,
        platform: str = DOMAIN,
        domain: str = "device_tracker",
        device_id: str | None = None,
        disabled_by: Any | None = None,
    ) -> SimpleNamespace:
        """Insert a synthetic entity registry entry."""

        self._next_id += 1
        entry = SimpleNamespace(
            entity_id=f"{domain}.entity_{self._next_id}",
            platform=platform,
            domain=domain,
            device_id=device_id,
            disabled_by=disabled_by,
        )
        self.entries.append(entry)
        return entry


def make_hass_stub(*, with_setdefault_error: bool = False) -> MagicMock:
    """Return a Home Assistant stub with ``hass.data`` and ``hass.config_entries``.

    ``with_setdefault_error`` triggers the defensive branch in
    :meth:`RegistryOperations._sync_owner_index` (B3) where
    ``hass.data.setdefault`` raises.
    """

    # spec_set whitelists the two attributes the mixin actually reads on hass.
    # An empty list would have blocked every assignment below (AttributeError).
    hass = MagicMock(spec_set=["data", "config_entries"])
    hass.data = {} if not with_setdefault_error else _RaisingDict()
    hass.config_entries = MagicMock()
    hass.config_entries.async_get_entry = MagicMock(return_value=None)
    return hass


class _RaisingDict(dict[str, Any]):
    """``hass.data`` variant whose ``setdefault`` raises (defensive-guard test)."""

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise RuntimeError("simulated hass.data corruption")


class RegistryStub(RegistryOperations):
    """Minimal :class:`RegistryOperations` subclass for isolated mixin tests.

    Cross-mixin ``_MixinBase`` stubs are seeded as ``MagicMock`` so methods
    under test can call them transparently; tests rebind per-case via
    :func:`setattr`. No ``super().__init__`` chain runs because
    ``DataUpdateCoordinator.__init__`` would require a full HA runtime.
    """

    def __init__(
        self,
        hass: Any | None = None,
        config_entry: Any | None = None,
        *,
        data: list[dict[str, Any]] | None = None,
    ) -> None:
        # _MixinBase declares these as plain annotations; assign at runtime.
        self.hass = hass if hass is not None else make_hass_stub()
        self.config_entry = config_entry
        self.data = data if data is not None else []

        # Caches populated lazily by the mixin.
        self._device_registry_config_subentry_kwarg_cache = None
        self._device_registry_supports_translation_update = None
        self._device_names: dict[str, str] | None = None

        # Owner-index / poll-target sets the mixin reads and writes.
        self._devices_with_entry: set[str] = set()
        self._enabled_poll_device_ids: set[str] = set()
        self._identity_key_to_devices: dict[bytes, set[str]] = {}

        # Service-device bookkeeping (read by M15 in PR 1.A.2).
        self._service_device_ready = False
        self._service_device_id = None

        # Subentry metadata mapping (typed on _MixinBase; runtime-empty here).
        self._subentry_metadata = {}

        # Cross-mixin methods (R-NEU-3): mock them so default behavior is a no-op.
        # Tests can override per-case via setattr().
        self._refresh_subentry_index = MagicMock(return_value=None)
        self._schedule_eid_resolver_refresh = MagicMock(return_value=None)
        self.get_subentry_metadata = MagicMock(return_value=None)
        self.stable_subentry_identifier = MagicMock(return_value=TRACKER_SUBENTRY_KEY)
        self.get_device_display_name = MagicMock(return_value=None)
        self.increment_stat = MagicMock(return_value=None)


__all__ = [
    "RegistryStub",
    "_DevRegStub",
    "_EntRegStub",
    "make_hass_stub",
]

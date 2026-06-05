# tests/helpers/identity_mixin_stub.py
"""Test stub for :class:`IdentityOperations` mixin standalone unit tests.

PR 1.A.2 (Phase 2, AP-A): exercise the simple identity mixin methods
(``_get_account_email``, ``_create_auth_issue``, ``_dismiss_auth_issue``,
``_schedule_eid_resolver_refresh``, ``_register_identity_key``,
``_reset_resolver_offset``) without the full coordinator. ``_MixinBase`` is
runtime-empty, so a minimal subclass seeds every attribute the mixin reads
and pre-mocks cross-mixin methods (``_entry_id``) to dodge the
``NotImplementedError`` traps that come from ``_MixinBase``.

The complex ``get_active_device_identities`` (732 LOC, dozens of
collaborators) is intentionally out of scope here; it lands in a later AP.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from custom_components.googlefindmy.coordinator.identity import IdentityOperations


def make_hass_stub() -> MagicMock:
    """Return a Home Assistant stub with ``hass.data`` and ``async_create_task``.

    ``hass.data`` is a plain dict so tests can populate it with the
    ``DOMAIN`` -> ``{DATA_EID_RESOLVER: ...}`` shape that
    :meth:`IdentityOperations._schedule_eid_resolver_refresh` reads.
    """

    hass = MagicMock(spec_set=["data", "async_create_task"])
    hass.data = {}
    hass.async_create_task = MagicMock(return_value=None)
    return hass


class IdentityStub(IdentityOperations):
    """Minimal :class:`IdentityOperations` subclass for isolated mixin tests.

    Cross-mixin ``_MixinBase`` stubs (``_entry_id``) are seeded as
    ``MagicMock`` so methods under test can call them transparently; tests
    rebind per-case via :func:`setattr`. No ``super().__init__`` chain runs
    because ``DataUpdateCoordinator.__init__`` would require a full HA
    runtime.
    """

    def __init__(
        self,
        hass: Any | None = None,
        config_entry: Any | None = None,
    ) -> None:
        self.hass = hass if hass is not None else make_hass_stub()
        self.config_entry = config_entry

        # Shared-tracker bookkeeping (read+written by ``_register_identity_key``).
        self._identity_key_to_devices: dict[bytes, set[str]] = {}

        # Cross-mixin methods owned by RegistryOperations: return the entry's
        # ``entry_id`` (or None) by default so ``_reset_resolver_offset`` works
        # without extra wiring. Tests override per-case via setattr().
        entry_id = getattr(config_entry, "entry_id", None) if config_entry else None
        self._entry_id = MagicMock(return_value=entry_id)

        # Diagnostics buffer (``get_active_device_identities`` reads it; kept
        # here as None so its absence is well-defined for future tests).
        self._diag = None


__all__ = ["IdentityStub", "make_hass_stub"]

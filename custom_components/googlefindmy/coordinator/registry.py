"""Device registry operations for GoogleFindMyCoordinator.

This module will contain registry-related methods extracted from main.py
during Phase 2 of the refactoring.

Methods to be moved here (11 total, ~1,682 lines):
- _ensure_registry_for_devices
- _ensure_service_device_exists
- _find_tracker_entity_entry
- _reindex_poll_targets_from_device_registry
- _call_device_registry_api
- _get_device_by_canonical_id
- _update_device_registry_entry
- _remove_device_registry_entry
- _list_registered_devices
- _sync_device_registry
- _validate_registry_state
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class RegistryOperations:
    """Device registry operations mixin for GoogleFindMyCoordinator.

    This class will contain methods that manage device registry entries,
    including creation, updates, and synchronization of HA device registry
    with the Google Find My device list.

    Currently empty - methods will be extracted in Phase 2.
    """

    pass

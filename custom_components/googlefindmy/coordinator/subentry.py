"""Subentry operations for GoogleFindMyCoordinator.

This module will contain subentry-related methods extracted from main.py
during Phase 3 of the refactoring.

Methods to be moved here (17 total, ~766 lines):
- _refresh_subentry_index
- _schedule_core_subentry_repair
- _build_core_subentry_definitions
- attach_subentry_manager
- detach_subentry_manager
- _get_subentry_by_id
- _list_subentries
- _create_subentry
- _update_subentry
- _delete_subentry
- _validate_subentry
- _sync_subentries
- _migrate_legacy_subentries
- _repair_subentry_index
- _rebuild_subentry_cache
- _get_subentry_devices
- _link_device_to_subentry
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class SubentryOperations:
    """Subentry operations mixin for GoogleFindMyCoordinator.

    This class will contain methods that manage config entry subentries,
    including creation, updates, and synchronization of subentry indices
    with tracked devices.

    Currently empty - methods will be extracted in Phase 3.
    """

    pass

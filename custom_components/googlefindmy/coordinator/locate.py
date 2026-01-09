"""Locate operations for GoogleFindMyCoordinator.

This module will contain location-related methods extracted from main.py
during Phase 6 of the refactoring.

Methods to be moved here (~274+ lines):
- async_locate_device
- Additional *location* methods as identified
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class LocateOperations:
    """Locate operations mixin for GoogleFindMyCoordinator.

    This class will contain methods that handle device location requests,
    including the async locate API and related location management.

    Currently empty - methods will be extracted in Phase 6.
    """

    pass

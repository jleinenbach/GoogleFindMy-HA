"""Identity operations for GoogleFindMyCoordinator.

This module will contain identity-related methods extracted from main.py
during Phase 5 of the refactoring.

Methods to be moved here (4+ total, ~776 lines):
- get_active_device_identities
- _register_identity_key
- _normalize_identity_key
- _validate_identity_key
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class IdentityOperations:
    """Identity operations mixin for GoogleFindMyCoordinator.

    This class will contain methods that manage device identities,
    including identity key registration, normalization, and the
    active device identities collection.

    Currently empty - methods will be extracted in Phase 5.
    """

    pass

"""Coordinator package for GoogleFindMy integration.

This package contains the GoogleFindMyCoordinator class and related components.
All public symbols are re-exported here for backwards compatibility.

Architecture:
    The coordinator uses a mixin composition pattern.  Six Operations classes
    (RegistryOperations, SubentryOperations, LocateOperations,
    IdentityOperations, PollingOperations, CacheOperations) are composed into
    GoogleFindMyCoordinator via multiple inheritance.

    All mixins inherit from ``_MixinBase`` (defined in ``_mixin_typing.py``),
    a type-declaration-only base class that gives mypy visibility into the full
    coordinator interface without introducing runtime overhead.

Usage (unchanged):
    from .coordinator import GoogleFindMyCoordinator
"""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

# Re-exports for test monkeypatching compatibility
# These modules are imported in main.py and tests patch them via coordinator module
from ..api import GoogleFindMyAPI
from ..KeyBackup.cloud_key_decryptor import decrypt_eik

# Re-export helpers subpackage
from . import helpers

# Re-export stats classes from helpers (commonly needed)
from .helpers.stats import (
    ApiStatus,
    CryptoStatus,
    DiagnosticsBuffer,
    FcmStatus,
    StatusSnapshot,
)

# Operations mixin classes (all inherit from _MixinBase for strict typing)
from .identity import IdentityOperations
from .locate import LocateOperations

# Re-export main coordinator class and related types
from .main import (
    # Constants used by tests
    _FCM_FALLBACK_POLL_AFTER_S,
    _PREDICTION_BUFFER_S,
    # Data classes
    CacheProtocol,
    DeviceIdentity,
    # Main class
    GoogleFindMyCoordinator,
    SemanticLabelRecord,
    # Semi-public functions (used by other modules)
    _as_ha_attributes,
    _sync_get_last_gps_from_history,
    # Public functions
    format_epoch_utc,
    get_recorder,
    normalize_epoch_seconds,
)
from .polling import PollingOperations
from .registry import RegistryOperations

# Re-export SubentryMetadata from subentry module
from .subentry import SubentryMetadata, SubentryOperations

__all__ = [
    # Main class
    "GoogleFindMyCoordinator",
    # Operations classes
    "IdentityOperations",
    "LocateOperations",
    "PollingOperations",
    "RegistryOperations",
    "SubentryOperations",
    # Data classes
    "CacheProtocol",
    "DeviceIdentity",
    "SemanticLabelRecord",
    "SubentryMetadata",
    # Public functions
    "format_epoch_utc",
    "get_recorder",
    "normalize_epoch_seconds",
    # Semi-public functions
    "_as_ha_attributes",
    "_sync_get_last_gps_from_history",
    # Constants
    "_FCM_FALLBACK_POLL_AFTER_S",
    "_PREDICTION_BUFFER_S",
    # Stats classes
    "ApiStatus",
    "CryptoStatus",
    "DiagnosticsBuffer",
    "FcmStatus",
    "StatusSnapshot",
    # For monkeypatching
    "DataUpdateCoordinator",
    "GoogleFindMyAPI",
    "async_call_later",
    "decrypt_eik",
    "dr",
    "er",
    # Subpackage
    "helpers",
]

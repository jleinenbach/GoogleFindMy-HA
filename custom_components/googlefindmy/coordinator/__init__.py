"""Coordinator package for GoogleFindMy integration.

This package contains the GoogleFindMyCoordinator class and related components.
All public symbols are re-exported here for backwards compatibility.

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

# Operations classes - currently empty, will be filled in Phases 2-6
from .identity import IdentityOperations
from .locate import LocateOperations
from .polling import PollingOperations
from .registry import RegistryOperations
from .subentry import SubentryOperations

# Re-export stats classes from helpers (commonly needed)
from .helpers.stats import (
    ApiStatus,
    DiagnosticsBuffer,
    FcmStatus,
    StatusSnapshot,
)

# Re-export SubentryMetadata from subentry module
from .subentry import SubentryMetadata

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
